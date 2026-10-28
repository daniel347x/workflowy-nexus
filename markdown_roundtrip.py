import json
import re
import sys
import os
import argparse
from typing import Dict, Any, List, Optional
from markdown_it import MarkdownIt

# Force UTF-8 output for Windows console
if sys.platform == 'win32':
    os.system('chcp 65001 > nul')
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

"""
markdown_roundtrip.py (v2.0 - markdown-it-py + mdformat)

Purpose:
    Convert a Workflowy NEXUS JSON export back to Markdown using battle-tested
    markdown-it-py parser + mdformat's MDRenderer.

Key improvements over v1 (hand-rolled parser):
    - Properly handles nested code blocks (4+ backticks)
    - Preserves HTML entities correctly
    - CommonMark spec-compliant
    - No manual regex parsing - relies on proven libraries
    - Handles complex escaping automatically

Input format:
    NEXUS JSON export from workflowy_scry() / nexus_scry():
    {
        "export_root_id": "uuid",
        "export_root_name": "Node Name",
        "export_timestamp": timestamp,
        "nodes": [ ... NEXUS node tree ... ]
    }

Algorithm:
    1. Load NEXUS JSON
    2. Convert NEXUS tree → markdown-it token stream
    3. Render token stream → Markdown via MDRenderer
    4. Convert HTML entities back to Markdown syntax (<b> → **)

This produces a clean Markdown file that can be re-imported via Cartographer.
"""


# Sentinel node name for pre-first-heading "leading text" in a Markdown file.
# This is the reverse-side (Workflowy -> disk) twin of the forward constant
# nexus_map_codebase.MD_LEADING_TEXT_NODE_NAME; the two MUST stay byte-identical.
# A node with this name holds prose that lived ABOVE the first heading on disk.
# nexus_to_tokens emits its note body VERBATIM (no '#' heading) above the first
# heading, exactly mirroring how "⚙️ YAML Frontmatter" is emitted — so the
# round-trip never turns leading text into a heading and never accumulates.
MD_LEADING_TEXT_NODE_NAME = "📄 Leading text"


# Sentinel that marks a note body which F12+3 wrapped in a protective code
# fence because the body itself contains Markdown headings. Without the fence,
# injecting such a body verbatim beneath a heading on disk would let the body's
# '#' lines become first-class document headings (SQL-injection-style), which
# on the next F12+1 would explode into spurious, wrongly-leveled Workflowy
# nodes and corrupt the file structure. The sentinel is an HTML comment (never
# rendered by Markdown renderers) placed immediately above the opening fence,
# so the F12+1 read side can recognize OUR fence (vs. a fence that is genuinely
# the user's own content) and strip it back out — leaving the Workflowy note
# clean (no visible fences) in the general case.
NEXUS_FENCED_NOTE_SENTINEL = "<!-- @nexus-fenced-note -->"


# @beacon[
#   id=auto-beacon@_choose_backtick_fence-fnc1,
#   role=_choose_backtick_fence(text: str),
#   slice_labels=nexus-md-header-path,carto-rev-markdown-full,nexus--md-preview,
#   kind=ast,
#   comment=F12+3 note-fencing: pick a backtick fence longer than any run inside the body so the fence cannot be closed prematurely (CommonMark closing-fence rule). min 3 backticks.,
# ]
def _choose_backtick_fence(text: str) -> str:
    """Return a backtick fence guaranteed not to be closed by ``text``.

    CommonMark: a fenced code block opened with N backticks can only be closed
    by a line whose backtick run is >= N. So we scan ``text`` for the longest
    contiguous run of backticks anywhere and return one MORE than that (min 3).
    By construction no interior line can prematurely close the fence.
    """
    max_run = 0
    for m in re.finditer(r"`+", text or ""):
        max_run = max(max_run, len(m.group(0)))
    return "`" * max(3, max_run + 1)


# @beacon[
#   id=auto-beacon@_note_body_has_heading-fnc2,
#   role=_note_body_has_heading(note_body: str),
#   slice_labels=nexus-md-header-path,carto-rev-markdown-full,nexus--md-preview,
#   kind=ast,
#   comment=F12+3 note-fencing: detect whether a note body contains ANY Markdown heading, using the SAME monkey-patched markdown-it parser F12+1 uses (arbitrary hash depth, fence-aware) so detector == live recognizer.,
# ]
def _note_body_has_heading(note_body: str) -> bool:
    """Return True iff ``note_body`` parses to contain at least one heading.

    Uses the EXACT same parser configuration that ``parse_markdown_structure``
    uses on the F12+1 read side: a CommonMark markdown-it with the ATX heading
    rule replaced by ``markdown_it_heading_arbitrary_depth`` (6-hash cap lifted
    to 64). This guarantees the detector agrees with the live recognizer:

      - It is fence-aware, so a '#' line that is ALREADY inside a fenced code
        block in the note does NOT count as a heading (no false positive — the
        special case where the note's genuine content is a fenced block with a
        '#' inside it is correctly treated as headless).
      - It recognizes 7+ hash headings, matching Dan's deep-heading 99-files.

    Returns False on empty input or any parse error (fail-safe: when unsure we
    do NOT fence, preserving verbatim behavior for non-heading content).
    """
    if not isinstance(note_body, str) or not note_body.strip():
        return False
    try:
        import importlib
        cartographer = importlib.import_module("nexus_map_codebase")
        md = MarkdownIt("commonmark")
        md.block.ruler.at("heading", cartographer.markdown_it_heading_arbitrary_depth)
        tokens = md.parse(note_body)
        return any(getattr(t, "type", "") == "heading_open" for t in tokens)
    except Exception:
        return False


# @beacon[
#   id=auto-beacon@_maybe_fence_sentinel_note-fnc3,
#   role=_maybe_fence_sentinel_note(note_body: str),
#   slice_labels=nexus-md-header-path,carto-rev-markdown-full,nexus--md-preview,
#   kind=ast,
#   comment=F12+3 WRITE side: wrap a note body in sentinel + dynamic fence IFF it contains headings. Idempotent — unfences any prior wrap first so fences never accumulate across round trips.,
# ]
def _maybe_fence_sentinel_note(note_body: str) -> str:
    """Protect a heading-bearing note body with a sentinel + dynamic fence.

    Used on the F12+3 WRITE side (``nexus_to_tokens``). If ``note_body`` (after
    first reversing any prior wrap, for idempotency) contains a Markdown
    heading, wrap it as::

        <!-- @nexus-fenced-note -->
        ```` (chosen long enough)
        <note_body verbatim>
        ````

    so that when this body is injected beneath a heading on disk, the body's
    headings are inert fenced content rather than real document structure.
    If the body has no heading, it is returned UNCHANGED (verbatim), so genuine
    user content — including a note whose real content is itself a fenced code
    block — is never needlessly wrapped.
    """
    if not isinstance(note_body, str):
        return ""
    # Idempotency: strip any wrap we previously added before re-evaluating.
    body = _unfence_sentinel_note(note_body)
    if not _note_body_has_heading(body):
        return body
    fence = _choose_backtick_fence(body)
    return f"{NEXUS_FENCED_NOTE_SENTINEL}\n{fence}\n{body}\n{fence}"


# @beacon[
#   id=auto-beacon@_unfence_sentinel_note-fnc4,
#   role=_unfence_sentinel_note(note_body: str),
#   slice_labels=nexus-md-header-path,carto-rev-markdown-full,nexus--md-preview,
#   kind=ast,
#   comment=F12+1 READ side (and F12+3 idempotency): if a note body begins with OUR sentinel immediately followed by an opening fence, strip the sentinel + matching outer fences and return the clean interior. Only OUR sentinel-marked fence is stripped — a user's own fenced content is left intact. Bail-to-verbatim if no matching close.,
# ]
def _unfence_sentinel_note(note_body: str) -> str:
    """Reverse ``_maybe_fence_sentinel_note``: strip our sentinel + outer fence.

    Tight binding (so we never strip a coincidental inner fence): the sentinel
    must be the FIRST non-blank line, and an opening backtick fence must be the
    next non-blank line after it. We then consume to the matching closing fence
    (a line whose backtick run is >= the opener's) and return the interior
    verbatim. If anything doesn't match (no sentinel, no opening fence, or no
    matching close), return ``note_body`` UNCHANGED — the conservative bail.
    """
    if not isinstance(note_body, str) or NEXUS_FENCED_NOTE_SENTINEL not in note_body:
        return note_body if isinstance(note_body, str) else ""
    lines = note_body.splitlines()
    n = len(lines)
    # 1. First non-blank line must be exactly the sentinel.
    i = 0
    while i < n and not lines[i].strip():
        i += 1
    if i >= n or lines[i].strip() != NEXUS_FENCED_NOTE_SENTINEL:
        return note_body
    # 2. Next non-blank line must be an opening backtick fence.
    j = i + 1
    while j < n and not lines[j].strip():
        j += 1
    if j >= n:
        return note_body
    m_open = re.match(r"^(`{3,})\s*$", lines[j].strip())
    if not m_open:
        return note_body
    open_len = len(m_open.group(1))
    # 3. Find the matching closing fence (run of >= open_len backticks, nothing else).
    close_re = re.compile(r"^(`{" + str(open_len) + r",})\s*$")
    k = j + 1
    close_idx = None
    while k < n:
        if close_re.match(lines[k].strip()):
            close_idx = k
            break
        k += 1
    if close_idx is None:
        # No matching close — corrupted/edge case. Bail: return verbatim.
        return note_body
    interior = lines[j + 1:close_idx]
    return "\n".join(interior)


# @beacon[
#   id=auto-beacon@load_nexus_root-sycb,
#   role=• ƒ load_nexus_root(data),
#   slice_labels=nexus-md-header-path,carto-rev-markdown-full,
#   kind=ast,
# ]
def load_nexus_root(data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the root node from NEXUS JSON export format.
    
    VALIDATION: Enforces single-child requirement for round-trip.
    Multiple children indicate stale CARTOGRAPHER runs at same target UUID.
    
    (Test comment for MCP validation.)
    """
    # NEXUS export format (from workflowy_scry / nexus_scry)
    if "export_root_id" in data and "nodes" in data:
        nodes = data.get("nodes") or []
        if not nodes:
            raise ValueError("NEXUS export contains no nodes")
        
        if len(nodes) > 1:
            raise ValueError(
                f"❌ GOTCHA DETECTED: SCRY contains {len(nodes)} root nodes (expected 1).\n"
                f"This indicates multiple CARTOGRAPHER runs targeted the same UUID.\n"
                f"Root nodes found: {[n.get('name', 'NO_NAME')[:50] for n in nodes]}\n\n"
                f"ACTION REQUIRED:\n"
                f"  1. Manually delete stale nodes from Workflowy, OR\n"
                f"  2. SCRY from a deeper child node that has only one version\n\n"
                f"Refusing to proceed - cannot determine which version is current."
            )
        
        return nodes[0]  # First (and only) node is the root
    
    # Fallback: direct node tree
    if "name" in data:
        return data
    
    raise ValueError("Unrecognized JSON format - expected NEXUS export or direct node tree")


# @beacon[
#   id=auto-beacon@detach_yaml_frontmatter_child-7x3q,
#   role=• ƒ detach_yaml_frontmatter_child(node),
#   slice_labels=nexus-md-header-path,carto-rev-markdown-full,
#   kind=ast,
# ]
def detach_yaml_frontmatter_child(node: Dict[str, Any]) -> tuple[str | None, Dict[str, Any]]:
    """Return (frontmatter_text, node_without_frontmatter_child).

    Looks for YAML frontmatter in two places, in priority order:
      1. An immediate child node named ``⚙️ YAML Frontmatter`` (the canonical
         representation produced by ``parse_markdown_structure``). If found,
         the child is removed from the returned node copy and its note text
         is returned separately.
      2. (Fallback safety net, Dan 2026-04-30) The root node's own ``note``
         field, when it begins with a ``---`` fence followed by a closing
         ``---``. This catches the case where Workflowy lost the frontmatter
         child node (e.g. agent edits, F12 refresh quirks) but the YAML
         content survived inside the file root's note. Without this fallback,
         ``nexus_to_tokens`` would emit the YAML body as a regular Markdown
         heading, mangling the frontmatter (the root cause of the F12+3 99-file
         corruption observed Apr 2026).

    Returns (None, node) only when no frontmatter is detected by either path.
    """
    import sys

    if not isinstance(node, dict):
        return None, node

    children = node.get("children") or []
    if not isinstance(children, list):
        children = []

    frontmatter_text: str | None = None
    kept_children: list[Dict[str, Any]] = []
    found_child = False

    for child in children:
        if isinstance(child, dict) and (child.get("name") or "").strip() == "⚙️ YAML Frontmatter":
            if frontmatter_text is None:
                frontmatter_text = str(child.get("note") or "")
            found_child = True
            continue
        kept_children.append(child)

    if found_child:
        node_copy = dict(node)
        node_copy["children"] = kept_children
        return frontmatter_text, node_copy

    # Fallback: probe the root node's own note for a `---`-fenced YAML block.
    root_note = node.get("note") or ""
    if isinstance(root_note, str) and root_note:
        note_lines = root_note.splitlines()
        if note_lines and note_lines[0].strip() == "---":
            for idx in range(1, len(note_lines)):
                if note_lines[idx].strip() == "---":
                    fm_lines = note_lines[1:idx]
                    remaining_note_lines = note_lines[idx + 1 :]
                    fm_text = "\n".join(fm_lines).strip("\n") or None
                    print(
                        "[MD-ROUNDTRIP] detach_yaml_frontmatter_child: "
                        "recovered frontmatter from root-node note (no "
                        "'⚙️ YAML Frontmatter' child found). This indicates "
                        "the Workflowy frontmatter child went missing; "
                        "falling back to root-note YAML to avoid mangling.",
                        file=sys.stderr,
                    )
                    node_copy = dict(node)
                    # Strip the YAML block from the note so it isn't emitted twice.
                    node_copy["note"] = "\n".join(remaining_note_lines).lstrip("\n")
                    return fm_text, node_copy
            # First line was '---' but no closing '---' was found; treat as
            # normal text (don't risk mangling).

    return None, node


# @beacon[
#   id=auto-beacon@_extract_md_path_lines_from_note-z2m4,
#   role=• ƒ _extract_md_path_lines_from_note(note),
#   slice_labels=nexus-md-header-path,carto-rev-markdown-full,
#   kind=ast,
# ]
def _extract_md_path_lines_from_note(note: str | None) -> list[str]:
    """Extract raw MD_PATH heading lines from a Workflowy note.

    Bug #3 hardening (Dan, 2026-05-01): the MD_PATH section of a Workflowy
    Markdown note is supposed to contain only Markdown heading lines
    (lines starting with 1-6 '#' characters followed by whitespace and
    heading text). However, an earlier corruption mode could leave
    BEACON metadata fields ('id: ...', 'role: ...', 'slice_labels: ...',
    'kind: ast') accidentally interleaved inside the MD_PATH block when
    the closing '---' separator was missing or misplaced. To make F12+3
    phase [6]/[7] resilient against such cache poisoning, this extractor
    now skips any line inside the MD_PATH block that does NOT match the
    Markdown heading shape '^#{1,32}\s+'. The block still terminates on
    the first '---' as before. This is layer (1) of the defense-in-depth
    fix; layers (2)/(3) live in nexus_to_tokens and the parse-time
    MD_PATH builder.
    """
    if not isinstance(note, str):
        return []
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
        path_lines.append(stripped)
    return path_lines


# @beacon[
#   id=auto-beacon@_heading_name_from_note_or_name-g7v1,
#   role=ƒ _heading_name_from_note_or_name(note, fallback_name: str),
#   slice_labels=nexus-md-header-path,carto-rev-markdown-full,
#   kind=ast,
# ]
def _heading_name_from_note_or_name(note: str | None, fallback_name: str) -> str:
    """Return the heading text to emit for a Workflowy heading node.

    Policy (Dan, 2026-04-30):
      Prefer the *Workflowy node name* (with decoration stripped) so that
      heading rename in Workflowy is reflected in the regenerated Markdown.
      The MD_PATH metadata block is treated as a stale snapshot; it gets
      refreshed on the next F12 file-refresh after F12+3 anyway.

      Decoration that gets stripped from raw_name:
        - Trailing ' 🔱' / '🔱' (trident marker added by apply_markdown_beacons).
        - Trailing '#tag' tokens (slice-label tags appended to beaconed names).

      If the decoration-stripped node name is empty (shouldn't happen, but
      guard anyway), fall back to the MD_PATH last-segment text. If both
      are empty, return the raw fallback_name.

    History:
      Previously this function preferred MD_PATH last-segment text over the
      node name on the rationale that decorated node names (🔱 + #tags)
      should not leak into headings. That made heading text effectively
      read-only in Workflowy (renaming a node had no effect on the emitted
      Markdown heading). Stripping the decoration explicitly fixes both
      problems.
    """
    import re as _re_local  # local alias to avoid shadowing module-level re

    raw = (fallback_name or "").strip()
    md_path_lines = _extract_md_path_lines_from_note(note)

    # Layer (2) heading-name guard (Dan, 2026-05-01, Bug #3 hardening):
    # Reject a fallback_name that smells like a leaked BEACON metadata
    # block. Such a name (e.g., "BEACON (MD AST)\nid: ...\nrole: ...\n...")
    # would otherwise be flattened to a single line via raw.split()/join(),
    # producing a corrupt heading like
    # '## BEACON (MD AST) id: ... role: ... slice_labels: ... kind: ast'
    # on disk, which is the original Bug #3 corruption shape.
    #
    # Heuristics for "smelly":
    #   - Contains an internal newline, OR
    #   - Starts with the BEACON header marker (any language), OR
    #   - Contains two or more recognizable metadata-field prefixes
    #     ('id:', 'role:', 'slice_labels:', 'kind:').
    def _name_is_meta_block(candidate: str) -> bool:
        if not candidate:
            return False
        if "\n" in candidate or "\r" in candidate:
            return True
        head = candidate.lstrip()
        if head.startswith("BEACON ("):
            return True
        meta_markers = ("id:", "role:", "slice_labels:", "kind:")
        hits = sum(1 for m in meta_markers if m in candidate)
        if hits >= 2:
            return True
        return False

    if _name_is_meta_block(raw):
        # Discard the poisoned name and fall through to MD_PATH fallback.
        raw = ""

    # Strip Workflowy decorations from the node name to get the canonical
    # heading text the user typed.
    if raw:
        # Strip trailing '#tag' tokens (whitespace-separated).
        tokens = raw.split()
        while tokens and tokens[-1].startswith("#"):
            tokens.pop()
        # Strip trailing '🔱' marker (also handle stray whitespace before it).
        while tokens and tokens[-1].strip() == "🔱":
            tokens.pop()
        # Also strip a '🔱' that's glued to the last token without whitespace.
        if tokens:
            last = tokens[-1]
            if last.endswith("🔱"):
                tokens[-1] = last[:-1].rstrip()
                if not tokens[-1]:
                    tokens.pop()
        canonical_name = " ".join(tokens).strip()
        if canonical_name:
            return canonical_name

    # Fallback: MD_PATH last-segment text (legacy behavior).
    if md_path_lines:
        last = md_path_lines[-1].strip()
        m = _re_local.match(r"^#{1,32}\s*(.*)$", last)
        if m:
            text = (m.group(1) or "").strip()
            # Same meta-block guard on the MD_PATH-derived fallback text:
            # if MD_PATH itself was poisoned at some point, layer (1) should
            # have already filtered the offending lines, but belt-and-suspenders.
            if text and not _name_is_meta_block(text):
                return text
        if last and not _name_is_meta_block(last):
            return last

    return raw


# @beacon[
#   id=auto-beacon@_strip_md_path_block-7s8k,
#   role=_strip_md_path_block(note),
#   slice_labels=nexus-md-header-path,carto-rev-markdown-full,nexus--md-preview,
#   kind=ast,
# ]
def _strip_md_path_block(note: str | None) -> str:
    """Remove the leading MD_PATH block from a Workflowy Markdown note."""
    if not isinstance(note, str):
        return ""
    note_lines = note.splitlines()
    if note_lines and note_lines[0].strip() == "MD_PATH:":
        cut = 1
        while cut < len(note_lines):
            if note_lines[cut].strip() == "---":
                cut += 1
                break
            cut += 1
        while cut < len(note_lines) and not note_lines[cut].strip():
            cut += 1
        return "\n".join(note_lines[cut:])
    return note


# @beacon[
#   id=auto-beacon@_strip_md_ast_beacon_block-1c9p,
#   role=• ƒ _strip_md_ast_beacon_block(note_text),
#   slice_labels=nexus-md-header-path,carto-rev-markdown-full,
#   kind=ast,
#   comment=Removes every scoped MD AST note-header record before prose generation so conflicting records are rehydrated separately rather than leaked as body text; ordinary body text remains after the header boundary.,
# ]
def _strip_md_ast_beacon_block(note_text: str | None) -> str:
    """Remove all scoped MD AST headers; their records belong to preparation.

    U03's note reader identifies actual headers and their blank/separator
    boundary. Several records are not one last-wins metadata/prose mixture.
    """
    from beacon_mutation_contract import parse_beacon_note_metadata
    if not isinstance(note_text, str):
        return ""

    lines = note_text.splitlines()
    removed = set()
    for record in parse_beacon_note_metadata(note_text):
        if not record["header"].startswith("BEACON (MD AST)"):
            continue
        start = record["line"] - 1
        end = start + 1
        while end < len(lines) and lines[end].strip() and lines[end].strip() != "---" and not lines[end].strip().startswith("BEACON ("):
            end += 1
        if end < len(lines) and lines[end].strip() == "---":
            end += 1
        removed.update(range(start, end))
    cleaned = [row for idx, row in enumerate(lines) if idx not in removed]

    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    return "\n".join(cleaned)


# @beacon[
#   id=auto-beacon@_is_markdown_span_beacon_node-s4q6,
#   role=• ƒ _is_markdown_span_beacon_node(node),
#   slice_labels=nexus-md-header-path,carto-rev-markdown-full,
#   kind=ast,
#   comment=Recognizes MD SPAN navigation records only in the shared positional metadata run; body/fenced lookalikes cannot omit an ordinary heading or its descendants.,
# ]
def _is_markdown_span_beacon_node(node: Dict[str, Any]) -> bool:
    """Omit navigation spans only when the positional header run identifies them.

    Pure preparation accounts for them. Once the shared reader leaves metadata,
    even a body/fenced line starting with the header cannot hide a heading or
    its descendants.
    """
    from beacon_mutation_contract import parse_beacon_note_metadata
    return any(r["header"].startswith("BEACON (MD SPAN)")
               for r in parse_beacon_note_metadata(node.get("note")))


# @beacon[
#   id=auto-beacon@_collect_markdown_ast_beacon_nodes-w8d3,
#   role=ƒ _collect_markdown_ast_beacon_nodes(root_node),
#   slice_labels=nexus-md-header-path,carto-rev-markdown-full,nexus--tags,
#   kind=ast,
#   comment=Collects live heading paths in generator priority order with occurrence counted over every heading before filtering tagged candidates; optionally retains node and scoped source-record evidence.,
# ]
def _collect_markdown_ast_beacon_nodes(root_node: Dict[str, Any], *, with_identity=False):
    """Collect tagged headings after assigning ALL headings their live ordinal.

    Default callers retain the legacy (name, rebuilt_note, md_path) tuples.
    ``with_identity`` returns every heading, tagged or not, with node identity,
    source records, verbatim original note and occurrence. Order matches ``nexus_to_tokens``' stable
    priority walk, not a depth/path sort. Cached paths are evidence only; the
    generated target always uses the current ancestor chain.
    """
    from beacon_mutation_contract import parse_beacon_note_metadata
    from nexus_map_codebase import _markdown_path_identity

    collected = []
    counts = {}
    source_counts = {}

    def _name_has_trailing_tags(raw_name: str) -> bool:
        tokens = (raw_name or "").strip().split()
        return bool(tokens and tokens[-1].startswith("#"))

    def walk(node: Dict[str, Any], depth: int, ancestor_chain: list[str]) -> None:
        """Recurse, accumulating disk-emitted heading text at each depth."""
        if not isinstance(node, dict):
            return
        # F12+3 is Workflowy -> disk. Emoji-prefixed nodes (📝, 💡, 🌟, 📌, etc.)
        # added directly in Workflowy are heading content here and MUST be
        # written to disk. SPAN beacons are filtered because they are
        # Workflowy-only navigation aids, not heading content.
        if _is_markdown_span_beacon_node(node):
            return

        raw_name = (node.get("name") or "").strip()
        note = node.get("note") or ""

        # YAML Frontmatter at depth >=1 is emitted as ---/---/blank, not a
        # heading. Skip its descendants from the heading-tree walk.
        if depth >= 1 and raw_name in {"⚙️ YAML Frontmatter", MD_LEADING_TEXT_NODE_NAME}:
            return

        # Build the next chain. Depth 0 (FILE root) does NOT contribute.
        if depth >= 1:
            own_heading = _heading_name_from_note_or_name(note, raw_name)
            new_chain = list(ancestor_chain)
            new_chain.append(own_heading or raw_name)
        else:
            new_chain = ancestor_chain

        # Phase [7] should emit a Markdown AST beacon iff the live Workflowy
        # heading name currently has trailing #tags. Existing cached BEACON
        # metadata is used only as stable metadata for that write, not as the
        # existence condition.
        if depth >= 1 and new_chain:
            md_path_lines: list[str] = []
            for idx, ancestor_text in enumerate(new_chain):
                # Arbitrary heading depth: NEXUS Markdown supports up to 64
                # heading levels (do NOT clamp to 6). Clamping at 6 collapses
                # deep ancestor chains into ambiguous sibling H6 stacks, which
                # then fail to resolve uniquely in
                # _find_markdown_heading_insert_idx during F12+3 phase 7.
                level = min(idx + 1, 64)
                md_path_lines.append(f"{'#' * level} {ancestor_text}")
            rewritten_note = _rewrite_md_path_block_in_note(str(note), md_path_lines)
            key = _markdown_path_identity(md_path_lines)
            counts[key] = counts.get(key, 0) + 1
            source_path = _extract_md_path_lines_from_note(str(note)) or md_path_lines
            source_key = _markdown_path_identity(source_path)
            source_counts[source_key] = source_counts.get(source_key, 0) + 1
            collected.append({"name": raw_name, "note": rewritten_note, "md_path": md_path_lines,
                              "occurrence": counts[key], "node_id": node.get("id") or node.get("uuid"),
                              "tagged": _name_has_trailing_tags(raw_name),
                              "source_md_path": source_path, "source_occurrence": source_counts[source_key],
                              "source_note": str(note),
                              "source_records": parse_beacon_note_metadata(note)})

        for child in sorted((c for c in node.get("children") or [] if isinstance(c, dict)),
                            key=lambda c: c.get("priority", 999999)):
            if isinstance(child, dict):
                walk(child, depth + 1, new_chain)

    walk(root_node, 0, [])
    if with_identity:
        return collected
    return [(r["name"], r["note"], r["md_path"]) for r in collected if r["tagged"]]

# @beacon[
#   id=auto-beacon@_rewrite_md_path_block_in_note-4hni,
#   role=_rewrite_md_path_block_in_note,
#   slice_labels=carto-rev-markdown-full,
#   kind=ast,
# ]
def _rewrite_md_path_block_in_note(note: str, md_path_lines: list[str]) -> str:
    """Replace the ``MD_PATH:`` block in a Workflowy note with a fresh one.

    If the note has no existing ``MD_PATH:`` block, prepend a new one. The
    BEACON block and any body text after the MD_PATH block are preserved.
    """
    if not isinstance(note, str):
        note = ""

    new_md_path_block_lines = ["MD_PATH:"]
    new_md_path_block_lines.extend(md_path_lines)
    new_md_path_block_lines.append("---")

    note_lines = note.splitlines() if note else []

    # Find the existing MD_PATH: block (from "MD_PATH:" line to the next "---").
    md_path_start: int | None = None
    md_path_end: int | None = None
    for idx, line in enumerate(note_lines):
        if line.strip() == "MD_PATH:":
            md_path_start = idx
            for j in range(idx + 1, len(note_lines)):
                if note_lines[j].strip() == "---":
                    md_path_end = j
                    break
            break

    if md_path_start is not None and md_path_end is not None:
        # Replace existing block in place.
        rewritten = (
            note_lines[:md_path_start]
            + new_md_path_block_lines
            + note_lines[md_path_end + 1:]
        )
    else:
        # No MD_PATH block found; prepend.
        rewritten = new_md_path_block_lines + note_lines

    return "\n".join(rewritten)


def _markdown_generation_analysis(snapshot):
    """Analyze supplied bytes through U05; an analyzer failure is data, not a veto."""
    import nexus_map_codebase as source
    from beacon_mutation_contract import parse_beacon_note_metadata
    from beacon_findings import beacon_snapshot_analysis_inputs, analyze_beacon_snapshot

    try:
        outline = source._beacon_attachment_outline(snapshot, "markdown")
        inputs = beacon_snapshot_analysis_inputs(
            snapshot, "markdown", source, outline=outline,
            parse_note=lambda text: [("beacon", r) for r in parse_beacon_note_metadata(text)])
        return {"findings": analyze_beacon_snapshot(snapshot, **inputs,
                                                   producer="markdown.generate"),
                "analysis_error": None, "inputs": inputs}
    except Exception as error:
        return {"findings": [], "analysis_error": {
            "exception": f"{type(error).__name__}: {error}",
            "canonical_file": snapshot.canonical_path, "file_sha256": snapshot.sha256}, "inputs": None}


def _markdown_span_sources(root):
    """Retain the source nodes reverse Markdown deliberately does not materialize."""
    from beacon_mutation_contract import parse_beacon_note_metadata

    rows = []

    def walk(node, path):
        if not isinstance(node, dict):
            return
        if _is_markdown_span_beacon_node(node):
            rows.append({"node_id": node.get("id") or node.get("uuid"), "name": node.get("name"),
                         "source_md_path": _extract_md_path_lines_from_note(node.get("note")) or path,
                         "source_occurrence": None,
                         "source_note": str(node.get("note") or ""),
                         "source_records": parse_beacon_note_metadata(node.get("note")), "span": True})
            return
        for child in sorted((c for c in node.get("children") or [] if isinstance(c, dict)),
                            key=lambda c: c.get("priority", 999999)):
            child_path = path + [f"{'#' * min(len(path) + 1, 64)} " +
                                 _heading_name_from_note_or_name(child.get("note"), child.get("name") or "")]
            walk(child, path if _is_markdown_span_beacon_node(child) else child_path)

    walk(root, [])
    return rows


def _markdown_block_position(block, index, source):
    """Heading-relative location distinguishes actual attachment from containment."""
    anchor = block.get("anchor") or next((a for a in reversed(index["anchors"])
                                         if a["line"] < block["record"]["line"]), None)
    path = source._extract_md_path_from_note(anchor["node"].get("note")) if anchor else None
    same = [a for a in index["anchors"] if path and source._markdown_path_identity(
        source._extract_md_path_from_note(a["node"].get("note"))) == source._markdown_path_identity(path)]
    return {"md_path": path, "occurrence": same.index(anchor) + 1 if anchor in same else None,
            "heading_line": anchor["line"] if anchor else None,
            "attachment": "attached_heading" if block.get("anchor") else "containing_heading" if anchor else "file_leading",
            "heading_relative_line": ((block["record"]["read_end_line"] if block.get("anchor") else block["record"]["line"]) - anchor["line"]) if anchor else None,
            "intervening_prose_lines": list(block["gap"]["prose_lines"])}


# @beacon[
#   id=markdown-generation@_markdown_generation_omissions,
#   slice_labels=beacon-writeback,carto-rev-markdown-full,
#   kind=ast,
#   comment=Accounts for source beacons with original scanner fields raw metadata and verbatim node notes; matches physical heading plus occurrence rather than equal id alone and returns unsupported span omissions and prose-relative AST relocation rows with manual actions; no relocation policy or IO.,
# ]
def _markdown_generation_omissions(previous, final, root, headings, outcomes):
    """Account for source records individually, never correlate solely by equal id.

    Historical rows are not executable current findings. They retain raw source
    metadata for manual restoration, and say explicitly that span regeneration
    remains unsupported. No relocation or keeper policy is inferred here.
    """
    import nexus_map_codebase as source

    def index(snapshot):
        structures = source._beacon_selection_structures(snapshot, "markdown")
        return source.beacon_attachment_index(snapshot, structures["outline"], structures)

    before, after = index(previous), index(final)
    source_records = source._beacon_edit_records(previous, "markdown")
    notes = []
    for heading in [*headings, *_markdown_span_sources(root)]:
        for number, metadata in enumerate(heading["source_records"]):
            notes.append({"heading": heading, "record_index": number, "metadata": metadata,
                          "emitted": any(o["node_index"] == headings.index(heading) and
                                         number in o.get("emitted_records", []) for o in outcomes)
                          if heading in headings else False})
    used, rows, relocations = set(), [], []
    actions = [{"resolution": "keep", "description": "Acknowledge this regeneration outcome; no persistent waiver."},
               {"action": "manual", "description": "Restore or relocate the recorded source metadata manually, then refresh. Reverse Markdown does not regenerate spans."}]
    for block in before["blocks"]:
        record, fields = block["record"], block["record"]["fields"]
        position = _markdown_block_position(block, before, source)
        matches = []
        for n, entry in enumerate(notes):
            heading, metadata = entry["heading"], entry["metadata"]
            if n in used or metadata.get("id") != fields.get("id"):
                continue
            if block.get("anchor"):
                if heading.get("span") or source._markdown_path_identity(heading["source_md_path"]) != source._markdown_path_identity(position["md_path"] or []):
                    continue
                if heading["source_occurrence"] != position["occurrence"]:
                    continue
            elif not heading.get("span"):
                continue
            matches.append(n)
        exact = [n for n in matches if all(notes[n]["metadata"].get(k) == fields.get(k)
                                         for k in ("comment", "slice_labels") if k in notes[n]["metadata"])]
        match = (exact or matches)[0] if len(exact or matches) == 1 else None
        entry = notes[match] if match is not None else None
        if match is not None:
            used.add(match)
        heading = entry["heading"] if entry else {}
        original = {"fields": dict(fields), "raw_bytes_hex": previous.raw_bytes[
            record["wrapper_range"][0] if record.get("wrapper_range") else record["opener_range"][0]:
            record["wrapper_range"][1] if record.get("wrapper_range") else record["read_end"]].hex(),
                    "record": record, "note_record": entry["metadata"] if entry else None,
                    "source_note": heading.get("source_note"),
                    "paired_closes": [{"record": r, "raw_bytes_hex": previous.raw_bytes[
                        r["wrapper_range"][0] if r.get("wrapper_range") else r["opener_range"][0]:
                        r["wrapper_range"][1] if r.get("wrapper_range") else r["read_end"]].hex()}
                        for r in source_records if r["marker"] == "close" and r.get("opener_line") == record["line"]]}
        common = {"node_id": heading.get("node_id"), "name": heading.get("name"),
                  "file": previous.canonical_path, "id": fields.get("id"),
                  "kind": fields.get("kind"), "effective_kind": block["effective_kind"],
                  "owning_heading": position, "original_metadata": original,
                  "available_actions": actions}
        if entry and entry["emitted"]:
            targets = [b for b in after["blocks"] if b["record"]["fields"].get("id") == fields.get("id")
                       and source._markdown_path_identity((_markdown_block_position(b, after, source)["md_path"] or [])) == source._markdown_path_identity(heading["md_path"])
                       and _markdown_block_position(b, after, source)["occurrence"] == heading["occurrence"]]
            if targets:
                final_position = _markdown_block_position(targets[0], after, source)
                if position["intervening_prose_lines"] or any(position[k] != final_position[k] for k in ("md_path", "occurrence")):
                    relocations.append({**common, "reason": "re_emitted_at_generated_heading",
                                        "original_position": position, "final_position": final_position,
                                        "target": {"node_id": heading.get("node_id"), "md_path": heading["md_path"],
                                                   "occurrence": heading["occurrence"]}})
                continue
        rows.append({**common, "reason": "demoted_ast_not_re_emitted" if fields.get("kind") == "ast" and not block.get("anchor")
                     else "implicit_span_not_re_emitted" if not fields.get("kind") and not block.get("anchor")
                     else "reverse_markdown_span_not_supported" if block["effective_kind"] == "span"
                     else "heading_tags_removed" if entry and not heading.get("tagged")
                     else "source_block_not_re_emitted"})
    for record in source_records:
        if record["marker"] != "close" or record.get("opener_line"):
            continue
        rows.append({"node_id": None, "name": None, "file": previous.canonical_path,
                     "id": record["fields"].get("id"), "kind": "close", "effective_kind": "close",
                     "owning_heading": None, "reason": "unassigned_close_not_re_emitted",
                     "original_metadata": {"fields": dict(record["fields"]), "record": record,
                         "raw_bytes_hex": previous.raw_bytes[record["opener_range"][0]:record["read_end"]].hex()},
                     "available_actions": actions})
    for n, entry in enumerate(notes):
        if n in used or entry["emitted"]:
            continue
        heading, metadata = entry["heading"], entry["metadata"]
        rows.append({"node_id": heading.get("node_id"), "name": heading.get("name"),
                     "file": previous.canonical_path, "id": metadata.get("id"),
                     "kind": metadata.get("kind"), "effective_kind": "span" if heading.get("span") else "ast",
                     "owning_heading": {"md_path": heading["source_md_path"], "occurrence": heading["source_occurrence"]},
                     "original_metadata": {"note_record": metadata, "source_note": heading.get("source_note")},
                     "reason": "reverse_markdown_span_not_supported" if heading.get("span") else
                     "heading_tags_removed" if not heading.get("tagged") else "heading_not_rehydrated",
                     "available_actions": actions})
    return rows, relocations


# @beacon[
#   id=markdown-generation@prepare_generated_markdown_beacons,
#   slice_labels=beacon-writeback,carto-rev-markdown-full,
#   kind=ast,
#   comment=Pure generated-document preparation: live all-heading identity then heading-authoritative U03 plans with each scoped record retained; U05 duplicate choices and omission/relocation evidence precede physical EOF normalization and one U02 publication plan; no IO or leases.,
# ]
def prepare_generated_markdown_beacons(generated_bytes, source_forest, previous_snapshot, *, source_exists=True):
    """Prepare phase 6 input, all heading metadata, findings and EOF without IO.

    ``previous_snapshot`` is the publication expectation observed BEFORE phase
    6, not a fresh read of an intermediate file. Existing target BOM is retained;
    otherwise the supplied generated BOM is retained. Generated physical line
    endings remain intact; inserted metadata uses U02's local-ending policy.
    Failures are per-heading outcomes; findings never gate independent work.
    J must join the protected phase-6 caller to activate whole-gesture atomicity.
    """
    import hashlib
    import nexus_map_codebase as source
    from beacon_source import source_snapshot_from_bytes
    from beacon_edits import BeaconEdit, BeaconEditPlan, verify_beacon_edit_plan
    from beacon_findings import make_beacon_finding

    root = source_forest if isinstance(source_forest, dict) else {"children": list(source_forest)}
    headings = _collect_markdown_ast_beacon_nodes(root, with_identity=True)
    raw = bytes(generated_bytes)
    if previous_snapshot.had_bom and not raw.startswith(b"\xef\xbb\xbf"):
        raw = b"\xef\xbb\xbf" + raw
    current = source_snapshot_from_bytes(raw, previous_snapshot.canonical_path)
    taken = {o["value"] for snap in (previous_snapshot, current)
             for r in source._beacon_edit_records(snap, "markdown") for o in r["occurrences"] if o["key"] == "id"}
    taken.update(o["value"] for h in headings for r in h["source_records"]
                 for o in r["occurrences"] if o["key"] == "id")
    outcomes = []
    for node_index, heading in enumerate(headings):
        if not heading["tagged"]:
            continue
        outcome = {k: heading[k] for k in ("node_id", "name", "md_path", "occurrence", "source_records")}
        outcome.update(node_index=node_index, file_path=current.canonical_path, language="markdown",
                       operation="noop", success=True, records=[], emitted_records=[])
        records = heading["source_records"] or [None]
        edits = []
        for number, record in enumerate(records):
            row = {"record_index": number, "beacon_id": (record or {}).get("id"), "operation": "noop"}
            try:
                structures = source._beacon_selection_structures(current, "markdown")
                selection = source.select_beacon_block_for_node(current, structures, {
                    "language": "markdown", "md_path": heading["md_path"], "occurrence": heading["occurrence"],
                    "note_beacon_id": (record or {}).get("id"), "operation": "rehydrate"})
                # A unique remaining path is not evidence for an out-of-range
                # ordinal supplied by the live all-heading walk.
                available = [a for a in source.beacon_attachment_index(current, structures["outline"], structures)["anchors"]
                             if source._markdown_path_identity(source._extract_md_path_from_note(a["node"].get("note"))) == source._markdown_path_identity(heading["md_path"])]
                if heading["occurrence"] > len(available):
                    row.update(success=False, error_kind="no_beacon_anchor_for_node", error="The generated heading occurrence is absent; no id-only retarget is authorized.")
                else:
                    intent = {"kind": "rehydrate", "name": heading["name"], "note": heading["note"],
                              "note_metadata": [record] if record else []}
                    mutation = source.plan_beacon_node_mutation(current, selection, intent)
                    if mutation.get("success") and mutation.get("planned_operation") == "create":
                        bid = (record or {}).get("id")
                        if not bid:
                            identity = json.dumps([heading["node_id"], heading["md_path"], heading["occurrence"], number], ensure_ascii=False)
                            bid = source.allocate_beacon_id(taken, base_id="auto-beacon@" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8])
                        taken.add(bid)
                        mutation = source.plan_beacon_node_mutation(current, selection, {**intent, "create_id": bid})
                    row.update({k: v for k, v in mutation.items() if k not in {"edit_plan", "selection", "target_key"}})
                    plan = mutation.get("edit_plan")
                    if row.get("success") and plan is not None:
                        proof = verify_beacon_edit_plan(plan, scanner=source._beacon_edit_records)
                        if proof["success"]:
                            edits.extend(plan.edits)
                        else:
                            row.update({k: v for k, v in proof.items() if k != "offset_map"})
                    if row.get("success"):
                        row["operation"] = {"create": "created_beacon", "update": "updated_beacon"}.get(mutation.get("planned_operation"), "noop")
                        if record:
                            outcome["emitted_records"].append(number)
            except Exception as error:
                row.update(success=False, error_kind="heading_preparation_failed", error=f"{type(error).__name__}: {error}")
            outcome["records"].append(row)
        # Distinct header records on ONE heading remain distinct blocks. Their
        # common insertion slot is concatenated in note order, never coalesced
        # by id. Every component above has already passed U02's bounded proof.
        combined = []
        for edit in edits:
            if combined and edit.start_byte == edit.end_byte == combined[-1].start_byte == combined[-1].end_byte:
                last = combined.pop()
                combined.append(BeaconEdit(last.start_byte, last.end_byte, last.replacement + edit.replacement, "document"))
            else:
                combined.append(BeaconEdit(edit.start_byte, edit.end_byte, edit.replacement, "document"))
        if combined:
            proof = verify_beacon_edit_plan(BeaconEditPlan(current, "markdown", tuple(combined),
                permitted_structural_effects=("whole_document",)), scanner=source._beacon_edit_records)
            if proof["success"]:
                current = source_snapshot_from_bytes(proof["candidate_bytes"], current.canonical_path)
            else:
                outcome.update(success=False, error=proof.get("error"), error_kind=proof.get("error_kind"), emitted_records=[])
        outcome["success"] = outcome["success"] and all(r.get("success") for r in outcome["records"])
        if len(outcome["records"]) == 1:
            outcome.update({k: v for k, v in outcome["records"][0].items() if k != "record_index"})
        else:
            outcome["operation"] = "rehydrated_group" if outcome["success"] else "partial_group"
        outcomes.append(outcome)
    eof = _markdown_eof_preparation(current)
    final = source_snapshot_from_bytes(eof["final_bytes"], current.canonical_path)
    analysis = _markdown_generation_analysis(final)
    findings = analysis["findings"]
    if analysis["inputs"]:
        inputs = analysis["inputs"]
        for outcome in outcomes:
            if len(outcome["source_records"]) <= 1:
                continue
            members = [inputs["members"][b["owner"]] for b in inputs["attachment_index"]["blocks"]
                       if (p := _markdown_block_position(b, inputs["attachment_index"], source))["md_path"] and
                       source._markdown_path_identity(p["md_path"]) == source._markdown_path_identity(outcome["md_path"])
                       and p["occurrence"] == outcome["occurrence"]]
            existing = next((f for f in findings if f["code"] == "two_blocks_one_symbol" and
                             {m["opener_start_byte"] for m in f["members"]} == {m["opener_start_byte"] for m in members}), None)
            if existing:
                existing["detail"].update(source_records=outcome["source_records"], node_id=outcome["node_id"])
                continue
            findings.append(make_beacon_finding(final.canonical_path, members[0]["line"] if members else 0,
                "two_blocks_one_symbol", None, "This source heading carries several metadata records; choose or merge explicitly.",
                {"source_records": outcome["source_records"], "node_id": outcome["node_id"]},
                snapshot=final, members=members, producer="markdown.generate"))
    omissions, relocations = _markdown_generation_omissions(previous_snapshot, final, root, headings, outcomes)
    start = previous_snapshot.byte_offsets[0]
    replacement = final.raw_bytes[3:] if previous_snapshot.had_bom else final.raw_bytes
    edits = () if previous_snapshot.raw_bytes == final.raw_bytes else (
        BeaconEdit(start, len(previous_snapshot.raw_bytes), replacement, "document"),)
    publication_plan = BeaconEditPlan(previous_snapshot, "markdown", edits,
        permitted_structural_effects=("whole_document",), source_exists=source_exists)
    return {"final_bytes": final.raw_bytes, "outcomes": outcomes, "omissions": omissions,
            "relocations": relocations, "findings": findings, "analysis_error": analysis["analysis_error"],
            "publication_plan": publication_plan,
            "publication_policy": {"expected_sha256": previous_snapshot.sha256, "source_exists": source_exists,
                "bom": "retain_existing_or_generated", "had_bom": final.had_bom,
                "newlines": "retain_generated_physical_endings_with_local_metadata_and_eof", "line_endings": final.line_endings}}


def _markdown_eof_preparation(snapshot):
    """The text, file and whole-generation EOF adapters consume U02's ONE rule."""
    from beacon_edits import markdown_eof_edit_plan, verify_beacon_edit_plan
    plan = markdown_eof_edit_plan(snapshot)
    proof = verify_beacon_edit_plan(plan)
    if not proof["success"]:
        return {**proof, "edit_plan": None}
    before = len(snapshot.raw_bytes.decode("utf-8"))
    final = proof["candidate_bytes"]
    after = len(final.decode("utf-8"))
    return {"success": True, "changed": bool(plan.edits), "edit_plan": plan, "final_bytes": final,
            "before_chars": before, "after_chars": after, "delta_chars": after - before}


def _normalize_generated_markdown_eof(text):
    """Pure str compatibility surface over the byte/physical-row EOF authority."""
    from beacon_source import source_snapshot_from_bytes
    if not isinstance(text, str) or not text:
        return ""
    return _markdown_eof_preparation(source_snapshot_from_bytes(text.encode("utf-8"), "generated.md"))["final_bytes"].decode("utf-8")


# @beacon[
#   id=markdown-generation@_plan_markdown_duplicate_resolution,
#   slice_labels=beacon-writeback,carto-rev-markdown-full,
#   kind=ast,
#   comment=Pure phase-7.5 authority returns U05 current duplicate choices without renaming and delegates only a caller-selected finding resolution to U06's paired byte plan; projects chosen physical opener counts without an automatic keeper.,
# ]
def _plan_markdown_duplicate_resolution(snapshot, selected_resolution=None):
    """U05 current choices, optionally ONE explicit U06 request; never a keeper policy.

    Request shape: finding_id, resolution and params from a current finding.
    A stale/unoffered request is local data. Counts and rename rows are only
    projections of the shared resolver's chosen physical opener states.
    """
    from beacon_resolutions import plan_beacon_resolution
    import nexus_map_codebase as source

    analysis = _markdown_generation_analysis(snapshot)
    findings = analysis["findings"]
    duplicates = [f for f in findings if f["code"] == "duplicate_id"]
    result = {"changed": False, "duplicates": [{"beacon_id": f["beacon_id"],
        "count": len(f["members"]), "renamed": []} for f in duplicates], "renamed_count": 0,
        "findings": findings, "ambiguities": duplicates, "analysis_error": analysis["analysis_error"],
        "edit_plan": None, "planned_renames": []}
    if selected_resolution is None:
        return result
    finding = next((f for f in findings if f["finding_id"] == selected_resolution.get("finding_id")), None)
    if finding is None:
        return {**result, "success": False, "error_kind": "finding_not_found",
                "error": "The selected finding is not current; re-list and choose its physical members."}
    resolved = plan_beacon_resolution(snapshot, finding, selected_resolution.get("resolution"),
                                      selected_resolution.get("params"), language="markdown")
    result.update({k: v for k, v in resolved.items() if k != "finding"})
    result["changed"] = False  # a pure plan is not a committed rename
    plan = resolved.get("edit_plan")
    if plan is not None:
        records = {r["opener_range"][0]: r for r in source._beacon_edit_records(snapshot, "markdown")}
        for state in plan.intended_state:
            old = records.get(state["owner"])
            new_id = state.get("fields", {}).get("id")
            if old and old["marker"] == "open" and new_id and new_id != old["fields"].get("id"):
                result["planned_renames"].append({"from": old["fields"].get("id"), "to": new_id,
                                                  "comment_line": old["line"]})
    return result


# @beacon[
#   id=markdown-generation@publish_generated_markdown_beacons,
#   slice_labels=beacon-writeback,carto-rev-markdown-full,
#   kind=ast,
#   comment=Publishes a prepared generated-document plan once through U02's proof temp fsync final-hash and atomic replacement; no lease nesting retry rollback marker or independent source writer.,
# ]
def publish_generated_markdown_beacons(plan, *, before_final_hash=None):
    """One atomic publication, against the preparation-start snapshot; no new claim.

    The caller retains its existing generation claim until this commit returns.
    A noncooperating save before the final hash leaves external bytes untouched.
    The final hash-to-replace instruction race is U02's stated T2 limitation.
    """
    from beacon_edits import commit_beacon_edit_plan
    return commit_beacon_edit_plan(plan, before_final_hash=before_final_hash)


# @beacon[
#   id=auto-beacon@reapply_markdown_ast_beacons-4n6x,
#   role=ƒ reapply_markdown_ast_beacons(file_path: str, root_node, log_callback),
#   slice_labels=nexus-md-header-path,ra-reconcile,ra-bulk-visible-apply,carto-js-ts-beacons,carto-rev-markdown-full,nexus--tags,
#   kind=ast,
#   comment=F12+3 compatibility adapter: one snapshot then pure all-heading rehydration and one U02 publication; returns every heading outcome and projects findings omissions and relocations through the legacy warnings channel. Protected phase 6 still writes early until J's R5 join.,
# ]
def reapply_markdown_ast_beacons(
    file_path: str,
    root_node: Dict[str, Any],
    log_callback: "Callable[[str], None] | None" = None,
) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
    """One-read/one-publication compatibility adapter over pure preparation.

    Results contain ALL ordered heading outcomes, including unresolved items.
    Warnings keep name/error/md_path/operation and add node/file/id and typed
    finding/omission/relocation data. The protected caller already copies these
    warnings into its job. Its old count calls every warning a skip; J's R5/R6
    projection must distinguish diagnostics from failed headings.

    This phase-7 adapter cannot observe the pre-phase-6 file: the protected
    pipeline has already written it. Forest-only omissions remain reportable;
    original disk-only omissions/positions require J to pass the earlier snapshot
    directly to ``prepare_generated_markdown_beacons`` before publishing once.
    No Workflowy calls, leases or per-heading disk writes occur here.
    """
    from beacon_source import read_source_snapshot

    def _emit_log(msg: str) -> None:
        # Best-effort log emit. Never let logging break the pipeline.
        if log_callback is None:
            return
        try:
            log_callback(msg)
        except Exception:  # noqa: BLE001
            pass

    def _short_name(s: str, n: int = 80) -> str:
        s = (s or "").strip()
        if len(s) <= n:
            return s
        return s[:n - 3] + "..."

    snapshot = read_source_snapshot(file_path)
    prepared = prepare_generated_markdown_beacons(snapshot.raw_bytes, root_node, snapshot)
    results = prepared["outcomes"]
    total_candidates = len(results)
    _emit_log(
        f"Phase 7: rehydrating Markdown AST beacons "
        f"({total_candidates} candidate(s) to process)."
    )

    committed = publish_generated_markdown_beacons(prepared["publication_plan"])
    warnings: list[Dict[str, Any]] = []
    for idx, result in enumerate(results, start=1):
        name, md_path = result["name"], result["md_path"]
        depth = len(md_path) if md_path else 0
        result["publication"] = committed
        if not committed["success"]:
            result.update(success=False, prepared_operation=result["operation"], operation="noop",
                          error=committed.get("error"), error_kind=committed.get("error_kind"))
        if not result["success"]:
            error = result.get("error") or "; ".join(r.get("error", "") for r in result["records"] if not r.get("success"))
            warnings.append({"name": name, "error": error, "md_path": md_path,
                "operation": result["operation"], "node_id": result["node_id"], "file_path": file_path,
                "beacon_id": result.get("beacon_id"), "beacon_ids": [r.get("beacon_id") for r in result["records"]], "outcome": result})
        for field_error in (e for r in result["records"] for e in r.get("field_errors", [])):
            warnings.append({"name": name, "error": "A metadata field remains unresolved.", "md_path": md_path,
                "operation": result["operation"], "file_path": file_path, "node_id": result["node_id"],
                "beacon_id": result.get("beacon_id"), "field_error": field_error})
        op = result["operation"] if result["success"] else "SKIPPED"
        beacon_id = str(result.get("beacon_id") or result.get("id") or "")
        beacon_id_part = f" id={beacon_id}" if beacon_id else ""
        _emit_log(
            f"  beacon[{idx}/{total_candidates}] depth={depth} "
            f"name={_short_name(name)!r} -> {op}{beacon_id_part}"
        )

    for kind, rows in (("finding", prepared["findings"]), ("omission", prepared["omissions"]),
                       ("relocation", prepared["relocations"])):
        for row in rows:
            warnings.append({"name": row.get("name") or row.get("beacon_id") or row.get("id") or file_path,
                "error": row.get("message") or row.get("reason"), "md_path": row.get("target", {}).get("md_path"),
                "operation": kind, "file_path": file_path, "type": "beacon_" + kind, kind: row})
    if prepared["analysis_error"]:
        warnings.append({"name": file_path, "error": "Beacon analysis failed; heading preparation still published.",
                         "md_path": None, "operation": "analysis_error", "analysis_error": prepared["analysis_error"]})
    if not committed["success"] and not results:
        warnings.append({"name": file_path, "error": committed.get("error"), "md_path": None,
                         "operation": "publication_failed", "publication": committed})
    written = sum(bool(r["success"]) for r in results)
    _emit_log(f"Phase 7 complete: {written} written, {total_candidates - written} skipped ({total_candidates} total).")
    return results, warnings


# @beacon[
#   id=auto-beacon@nexus_to_tokens-chbp,
#   role=nexus_to_tokens(node, depth: int),
#   slice_labels=nexus-md-header-path,carto-rev-markdown-full,nexus--md-preview,
#   kind=ast,
# ]
def nexus_to_tokens(node: Dict[str, Any], depth: int = 0) -> List[str]:
    """Convert NEXUS node tree to Markdown text while preserving MD semantics.

    CRITICAL:
    - Sort children by priority (lower first) to preserve document order.
    - Use MD_PATH as the canonical source of heading text, so Workflowy-only
      AST-beacon decorations (🔱 and trailing #tags) do not leak into headings.
    - Omit Markdown SPAN beacon child nodes entirely from reverse export.
    - Strip BEACON (MD AST) metadata blocks from note bodies; these are
      rehydrated separately as HTML comment beacon blocks after file write.
    """
    lines: List[str] = []
    raw_name = (node.get("name") or "").strip()
    note = node.get("note") or ""
    children = [child for child in (node.get("children") or []) if isinstance(child, dict)]

    # Sort children by priority (ascending) - lower values appear first.
    children_sorted = sorted(children, key=lambda c: c.get("priority", 999999))

    if depth >= 1 and raw_name == "⚙️ YAML Frontmatter":
        lines.append("---")
        if note:
            lines.append(str(note))
        lines.append("---")
        lines.append("")
        return lines

    if depth >= 1 and raw_name == MD_LEADING_TEXT_NODE_NAME:
        # "Leading text" is prose that lived ABOVE the first heading on disk.
        # Emit its note body VERBATIM with NO '#' heading marker, so it lands
        # back above the first heading exactly as authored. This mirrors the
        # frontmatter branch above and is the reverse twin of
        # nexus_map_codebase._extract_markdown_leading_text. Because we never
        # emit a heading here, the F12+1 -> F12+3 -> F12+1 round-trip is
        # idempotent: the leading text is re-parsed back into another
        # MD_LEADING_TEXT_NODE_NAME node, never accumulating a heading.
        if note:
            lines.append(str(note))
            lines.append("")
        return lines

    if depth == 0:
        frontmatter_children = [
            child for child in children_sorted
            if (child.get("name") or "").strip() == "⚙️ YAML Frontmatter"
        ]
        leading_text_children = [
            child for child in children_sorted
            if (child.get("name") or "").strip() == MD_LEADING_TEXT_NODE_NAME
        ]
        normal_children = [
            child for child in children_sorted
            if (child.get("name") or "").strip() != "⚙️ YAML Frontmatter"
            and (child.get("name") or "").strip() != MD_LEADING_TEXT_NODE_NAME
            and not _is_markdown_span_beacon_node(child)
            # NOTE: every non-SPAN child emits. F12+3 is Workflowy -> disk;
            # emoji-prefixed nodes (📝, 💡, 🌟, 📌, etc.) added directly in
            # Workflowy must be written to disk.
        ]

        # Emission order MUST match on-disk file order: frontmatter first, then
        # any leading text, then the heading sections. (Mirrors
        # parse_markdown_structure, which inserts frontmatter at priority 0 and
        # leading text at priority 1, above the first heading at priority 100+.)
        for child in frontmatter_children:
            lines.extend(nexus_to_tokens(child, depth + 1))
        for child in leading_text_children:
            lines.extend(nexus_to_tokens(child, depth + 1))
        for child in normal_children:
            lines.extend(nexus_to_tokens(child, depth + 1))
        return lines

    # depth >= 1: emit heading
    # Arbitrary heading depth: NEXUS Markdown supports up to 64 heading
    # levels (do NOT clamp to 6). Clamping at 6 collapses deep Workflowy
    # nesting into flat sibling H6 stacks on disk, which then makes the
    # phase 7 MD_PATH lookup ambiguous and aborts F12+3.
    level = min(depth, 64)
    heading_name = _heading_name_from_note_or_name(note, raw_name)
    heading = f"{'#' * level} {heading_name}" if heading_name else f"{'#' * level}"
    lines.append(heading)
    lines.append("")

    note_body = _strip_md_ast_beacon_block(_strip_md_path_block(note))
    # F12+3 injection guard: if this note body itself contains Markdown
    # headings, wrap it in a sentinel-marked code fence so its '#' lines do
    # NOT become real document headings once injected beneath this heading on
    # disk (which would explode into spurious wrongly-leveled nodes on the next
    # F12+1). Idempotent and a no-op for headless bodies (incl. a note whose
    # genuine content is already a user-authored fenced block). The matching
    # F12+1 read side strips our sentinel+fence so the Workflowy note stays
    # clean. See _maybe_fence_sentinel_note / _unfence_sentinel_note.
    note_body = _maybe_fence_sentinel_note(note_body)
    if note_body:
        lines.append(note_body)
        lines.append("")

    for child in children_sorted:
        # Every non-SPAN child emits (F12+3 must write emoji-prefixed
        # user-added subtrees to disk).
        if _is_markdown_span_beacon_node(child):
            continue
        lines.extend(nexus_to_tokens(child, depth + 1))

    return lines


# @beacon[
#   id=auto-beacon@clean_html_entities-rh2t,
#   role=• ƒ clean_html_entities(markdown_text: str),
#   slice_labels=nexus-md-header-path,carto-rev-markdown-full,
#   kind=ast,
# ]
def clean_html_entities(markdown_text: str) -> str:
    """Convert HTML entities back to Markdown syntax.
    
    The forward mapper (nexus_map_codebase.py) converts Markdown **bold**
    to HTML <b>bold</b> tags. This reverses that conversion.
    
    Also handles common HTML entities that should be Markdown.

    Entity decoding policy (Dan, 2026-05, &nbsp; round-trip fix):
      We intentionally do NOT use html.unescape() here. html.unescape is the
      full Python entity decoder and decodes EVERYTHING, including exotic
      entities like '&nbsp;' -> '\xa0' (a literal non-breaking-space CHARACTER),
      '&copy;' -> '©', '&mdash;' -> '—', etc. That silently corrupted Markdown
      *source* on every F12+3 write: a table cell authored as the six literal
      characters '&nbsp;' would be rewritten to a raw U+00A0 byte on disk.

      The reverse path's job is to reproduce Markdown SOURCE, not to render it.
      The forward encoder (encode_for_workflowy_api in api_client_core.py) emits
      a small transport-escape set for semantic '&', '<', '>', and Workflowy may
      return ordinary double quotes as '&quot;'. So we decode ONLY that narrow set
      (&amp;/&lt;/&gt;/&quot; plus numeric double-quote spellings), mirroring
      _dewhiten_text's deliberate whitelist policy while preserving source-only
      entities. All other entities ('&nbsp;', '&copy;', ...) survive verbatim as
      literal source text, byte-identical to what the user authored. This makes
      the full round trip symmetric:
        disk '&nbsp;' -> wire '&amp;nbsp;' -> Workflowy shows '&nbsp;'
          -> F12+3 decodes only '&amp;'->'&' -> disk '&nbsp;'  (identical)
        Workflowy '&quot;quoted&quot;' -> disk '"quoted"'
    """
    import re
    from nexus_map_codebase import decode_workflowy_transport_entities_for_markdown_source
    
    # Decode ONLY the shared narrow transport/readability entity set. This is
    # the same helper used by Phase 7 beacon metadata rehydration, so Markdown
    # body/frontmatter and beacon role/comment fields follow one policy.
    text = decode_workflowy_transport_entities_for_markdown_source(markdown_text)
    
    # Convert <b>text</b> → **text** (bold)
    text = re.sub(r'<b>(.*?)</b>', r'**\1**', text, flags=re.DOTALL)
    
    # Convert <i>text</i> → *text* (italic)
    text = re.sub(r'<i>(.*?)</i>', r'*\1*', text, flags=re.DOTALL)
    
    # Convert <em>text</em> → *text* (emphasis)
    text = re.sub(r'<em>(.*?)</em>', r'*\1*', text, flags=re.DOTALL)
    
    # Convert <strong>text</strong> → **text** (strong)
    text = re.sub(r'<strong>(.*?)</strong>', r'**\1**', text, flags=re.DOTALL)
    
    return text


# @beacon[
#   id=auto-beacon@main-am67,
#   role=main,
#   slice_labels=carto-rev-markdown-full,
#   kind=ast,
#   comment=Standalone JSON-to-Markdown CLI prepares all generated heading metadata findings omissions and EOF against one pre-generation target snapshot then publishes once through U02; reports actual heading and warning counts.,
# ]
def main() -> None:
    """Generate from a supplied forest with the same preparation/publication seam."""
    from beacon_source import read_source_snapshot, source_snapshot_from_bytes
    parser = argparse.ArgumentParser(
        description="Convert NEXUS JSON to normalized Markdown (round-trip test)"
    )
    parser.add_argument(
        "json_path",
        help="Path to NEXUS JSON file (enchanted_terrain.json or phantom_gem.json)"
    )
    parser.add_argument(
        "--output", "-o",
        required=True,
        help="Output Markdown file path"
    )
    
    args = parser.parse_args()
    
    nexus_path = args.json_path
    output_path = args.output
    try:
        previous = read_source_snapshot(output_path)
        source_exists = True
    except FileNotFoundError:
        previous = source_snapshot_from_bytes(b"", os.path.realpath(output_path))
        source_exists = False

    # Load NEXUS JSON
    with open(nexus_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    root = load_nexus_root(data)
    frontmatter_text, root_for_render = detach_yaml_frontmatter_child(root)
    
    print(f"\n{'='*60}")
    print(f"MARKDOWN ROUND-TRIP v2.1 (preserve raw Workflowy Markdown)")
    print(f"Root node: {root.get('name', 'NO_NAME')}")
    print(f"Root children: {len(root.get('children', []))}")
    print(f"{'='*60}\n")

    # Convert NEXUS tree → Markdown text (excluding YAML frontmatter child).
    raw_markdown = "\n".join(nexus_to_tokens(root_for_render, depth=0))

    # Parse once for validation/debug visibility, but DO NOT render back through
    # mdformat's MDRenderer. MDRenderer normalizes ordered-list markers to "1."
    # for every item, which destroys explicit numbering preserved in Workflowy
    # notes (e.g. 1/2/3/4 collapses to 1/1/1/1).
    md = MarkdownIt("commonmark")
    tokens = md.parse(raw_markdown)

    print(f"Parsed {len(tokens)} tokens from NEXUS tree")

    # Preserve the raw Workflowy-authored Markdown structure verbatim, while
    # still cleaning HTML entities/tags that may have come from Workflowy.
    final_markdown = clean_html_entities(raw_markdown)
    if frontmatter_text is not None:
        frontmatter_clean = clean_html_entities(frontmatter_text).strip("\n")
        body_clean = final_markdown.lstrip("\n")
        final_markdown = f"---\n{frontmatter_clean}\n---\n\n{body_clean}"
    
    prepared = prepare_generated_markdown_beacons(final_markdown.encode("utf-8"), root_for_render,
                                                 previous, source_exists=source_exists)
    publication = publish_generated_markdown_beacons(prepared["publication_plan"])
    if not publication["success"]:
        raise RuntimeError(publication.get("error") or "Markdown publication failed")

    print(f"\n{'='*60}")
    print(f"ROUND-TRIP COMPLETE")
    print(f"Output file: {output_path}")
    print(f"Output size: {len(prepared['final_bytes'].decode('utf-8'))} chars")
    print(f"Markdown AST beacons reapplied: {sum(bool(r['success']) for r in prepared['outcomes'])}")
    print(f"Findings: {len(prepared['findings'])}; omissions: {len(prepared['omissions'])}; relocations: {len(prepared['relocations'])}")
    for key in ("findings", "omissions", "relocations"):
        for row in prepared[key]:
            print(json.dumps({key: row}, ensure_ascii=False))
    for row in prepared["outcomes"]:
        if not row["success"] or row.get("field_errors"):
            print(json.dumps({"heading_outcome": row}, ensure_ascii=False))
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
