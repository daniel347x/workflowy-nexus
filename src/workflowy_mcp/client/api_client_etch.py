"""WorkFlowy API client - ETCH and tree export/import operations."""

import json
import sys
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from datetime import datetime

from ..config import get_runtime_subdir
from ..debug_log_rotation import append_debug_line
from ..carto_job_runtime import run_carto_cpu
from ..models import (
    NetworkError,
    NodeCreateRequest,
    NodeListRequest,
    RateLimitError,
)

from .api_client_core import (
    WorkFlowyClientCore,
    _ClientLogger,
    log_event,
    API_RATE_LIMIT_DELAY,
)

# Compatibility name is the task-local variable, never a mutable global dict.
_current_weave_context: ContextVar[str | None] = ContextVar("weave_json_file", default=None)


# @beacon[
#   id=carto-runtime@weave-log-scope,
#   slice_labels=ra-carto-jobs,ra-logging,
#   kind=ast,
#   comment=Sets and resets one task-local source JSON path for existing per-file logging; nested scopes restore their token even on failure and interleaved daemon jobs never save or overwrite another task log destination.,
# ]
@contextmanager
def weave_log_scope(json_file):
    token = _current_weave_context.set(json_file)
    try:
        yield
    finally:
        _current_weave_context.reset(token)


# @beacon[
#   id=auto-beacon@_log_to_file_helper-epud,
#   role=_log_to_file_helper,
#   slice_labels=ra-logging,ra-reconcile,ra-carto-jobs,
#   kind=ast,
#   comment=Best-effort task-local logging retains its original line bytes and global fallback; only the two daemon-wide logs rotate at 10 MiB with five generations through append_debug_line. Per-job and per-file destinations remain plain append.,
# ]
def _log_to_file_helper(message: str, log_type: str = "reconcile") -> None:
    """Append one line to the debug log chosen by call context (best-effort).

    Args:
        message: The message to log
        log_type: "reconcile" -> reconcile_debug.log
                    "etch"      -> etch_debug.log

    A daemon-wide global destination rotates at 10 MiB into five generations;
    per-weave, per-file and per-job destinations retain their exact line bytes,
    including the writer's original unconditional trailing newline.
    
    If the current task's weave JSON path is set, logs go to the same directory
    as the JSON file (tag-specific). Otherwise, falls back to global temp/.
    """
    try:
        filename = "reconcile_debug.log"
        if log_type == "etch":
            filename = "etch_debug.log"
        elif log_type == "nexus":
            filename = "nexus_debug.log"
        
        # Determine log directory (tag-specific if in WEAVE/F12 context)
        json_file = _current_weave_context.get()
        if json_file and os.path.exists(json_file):
            # Tag-specific: for general WEAVE, keep a single reconcile_debug.log
            # per JSON directory (as before). For F12 Cartographer per-file
            # refreshes, where source_json files live under
            # temp/cartographer_file_refresh, use a per-file debug log whose
            # name is derived from the JSON basename.
            base_dir = os.path.dirname(json_file)
            base_name = os.path.basename(json_file)
            if "cartographer_file_refresh" in os.path.normcase(base_dir):
                stem, _ext = os.path.splitext(base_name)
                log_path = os.path.join(base_dir, f"{stem}.{filename}")
            else:
                log_path = os.path.join(base_dir, filename)
        else:
            # Global fallback
            log_path = os.path.join(str(get_runtime_subdir("logs")), filename)
        
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        append_debug_line(log_path, f"[{ts}] {message}\n")
    except Exception:
        # Never let logging failures affect API behavior
        pass


# ---- ETCH node-object grammar (the ONE authority; consumed by the recursive
# preflight inside workflowy_etch, which every ETCH ingress reaches) ----------
# Field set mirrors exactly what create_tree reads off each node_data dict
# (name / note / layout_mode / position / children). Adding a field means
# adding it HERE and in create_tree together; a key absent from this set is
# rejected loudly before any write instead of being silently dropped.
ETCH_NODE_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"name", "note", "children", "layout_mode", "position"}
)
ETCH_LAYOUT_MODES: frozenset[str] = frozenset({"bullets", "todo", "h1", "h2", "h3"})
ETCH_POSITIONS: frozenset[str] = frozenset({"top", "bottom"})
ETCH_NODE_GRAMMAR = "{name, note?, children?, layout_mode?, position?}"

# Teaching hints for the unknown keys agents actually reach for. These are
# MESSAGE TEXT only -- there is no code path that honors any of them.
_ETCH_UNKNOWN_KEY_HINTS: dict[str, str] = {
    "content": "'content' is anchor.point.capture's TOP-LEVEL argument for a "
               "single raw-text capture; inside an ETCH node object the note "
               "body field is 'note'.",
    "body": "the note body field is 'note'.",
    "text": "the node title field is 'name'; the note body field is 'note'.",
    "title": "the node title field is 'name'.",
    "description": "the note body field is 'note'.",
    "layoutMode": "use snake_case 'layout_mode'.",
    "tags": "tags are never embedded in an ETCH node object; pass them via "
            "anchor.point.capture's tags= argument or the tag family after "
            "the create.",
    "id": "ETCH is append-only creation and never accepts UUIDs; to modify an "
          "existing node use node.edit / node.move.",
    "uuid": "ETCH is append-only creation and never accepts UUIDs; to modify "
            "an existing node use node.edit / node.move.",
}


def _etch_unknown_keys_message(node_path: str, unknown: "list[str]") -> str:
    """Index-precise unknown-key rejection text for one ETCH node object."""
    lines = [
        f"{node_path} has unknown field(s): {unknown}. "
        f"ETCH node grammar is {ETCH_NODE_GRAMMAR}; unknown fields are "
        "rejected (never silently dropped) and NO nodes were created."
    ]
    for key in unknown:
        hint = _ETCH_UNKNOWN_KEY_HINTS.get(key)
        if hint:
            lines.append(f"  - {key!r}: {hint}")
    return "\n".join(lines)


# @beacon[
#   id=core-crud@parse_etch_nodes_payload,
#   slice_labels=nexus-core-v1,node-ops,
#   kind=ast,
#   comment=THE single shared ETCH nodes-payload parser. Turns the incoming `nodes` (already-parsed list, OR a stringified-JSON payload) into a list, applying Strategy 1 (direct json.loads) then Strategy 2 (structure-aware trim after the last ']' to salvage trailing junk like a stray request-body '}'). workflowy_etch delegates to this, AND add_to_anchor's forest-structure peek uses it, so the tags/expand gating can NEVER parse the payload differently than ETCH itself does (no second, weaker parser). Returns (parsed_list|None, strategy_used|None, error|None): error set => unparseable by BOTH strategies.,
# ]
def parse_etch_nodes_payload(
    nodes: "list[dict[str, Any]] | str",
) -> tuple["list[Any] | None", "str | None", "str | None"]:
    """Parse an ETCH ``nodes`` payload into a list (the ONE shared parser).

    This is the SINGLE source of truth for turning the incoming ``nodes``
    argument into a Python list. ``workflowy_etch`` calls it, and so does
    ``add_to_anchor`` (to peek at forest structure for the tags/expand
    gating), so the two can never diverge on what a given input string parses
    to -- the crack that would otherwise let a Strategy-2-only payload be
    mis-gated by a bare ``json.loads`` peek.

    Behavior (unchanged from ETCH's long-battle-tested inline logic):
      * Already a list -> returned as-is (strategy=None).
      * A string -> Strategy 1 (direct ``json.loads``); on failure,
        Strategy 2 (find the last ``']'`` and re-parse up to it, salvaging a
        valid array followed by trailing junk such as a stray ``}`` from the
        request body). Only applied once.
      * Anything else / both strategies fail -> ``(None, None, error)``.

    Returns ``(parsed, strategy_used, error)``:
      * ``parsed``  -- the list on success, else ``None``.
      * ``strategy_used`` -- a human label when a string autofix fired
        (Strategy 1 / Strategy 2), else ``None`` (already a list / error).
      * ``error`` -- a message when BOTH strategies failed (else ``None``).
    """
    if isinstance(nodes, list):
        return nodes, None, None
    if not isinstance(nodes, str):
        return None, None, "Parameter 'nodes' must be a list or a JSON string"

    # Strategy 1: direct json.loads() on the full string.
    try:
        return json.loads(nodes), "Strategy 1: Direct json.loads()", None
    except json.JSONDecodeError as e:
        # Strategy 2: structure-aware trim after the last ']' (recover from
        # trailing junk after a syntactically complete list literal, e.g. an
        # outer '}' from the request body). Applied once.
        last_bracket = nodes.rfind("]")
        if last_bracket != -1:
            candidate = nodes[: last_bracket + 1]
            try:
                return (
                    json.loads(candidate),
                    "Strategy 2: Trim after last ']' (recover from trailing junk)",
                    None,
                )
            except json.JSONDecodeError as e2:
                return (
                    None,
                    None,
                    (
                        "Failed to parse stringified JSON after Strategy 1 and "
                        f"Strategy 2. Strategy 1 error={repr(e)}, "
                        f"Strategy 2 error={repr(e2)}"
                    ),
                )
        return (
            None,
            None,
            f"Failed to parse stringified JSON: no closing ']' found. "
            f"Original error={repr(e)}",
        )


# ARCHITECTURE NOTE - CLIENT INHERITANCE "NAMESPACE" CHAIN (see api_client_core.py
# for the canonical, fuller explanation).
# ---------------------------------------------------------------------------
# This is the MIDDLE link: Core <- Etch <- Nexus. Single inheritance is used
# here as code-organization (one logical client split across three files), not
# polymorphism - a mild anti-pattern that is NOT ideal but deliberately NOT
# worth refactoring now. Practical consequence to remember: because Nexus
# extends Etch, any STATELESS helper that BOTH Etch and Nexus/GLIMPSE need
# lives HERE (the lowest common ancestor) - e.g. _annotate_preview_ids_and_
# build_tree, the shared preview-tree renderer used by both ETCH previews AND
# GLIMPSE full-mode. So a "GLIMPSE-looking" helper sitting in this file is by
# design, not a misplacement (a parent class cannot see a child's methods).
# ---------------------------------------------------------------------------
# @beacon[
#   id=auto-beacon@WorkFlowyClientEtch-r1a9,
#   role=WorkFlowyClientEtch,
#   slice_labels=node-ops,
#   kind=ast,
# ]
class WorkFlowyClientEtch(WorkFlowyClientCore):
    """ETCH operations and tree export/import - extends Core."""

    def _log_to_file(self, message: str, log_type: str = "reconcile") -> None:
        """Log message to a tag-specific debug file (best-effort)."""
        _log_to_file_helper(message, log_type)

    async def export_nodes(
        self,
        node_id: str | None = None,
        max_retries: int = 10,
        use_cache: bool = True,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Export all nodes or filter to specific node's subtree.
        
        Delegates to module-level export_nodes_impl to avoid circular imports.
        """
        return await export_nodes_impl(
            self,
            node_id=node_id,
            max_retries=max_retries,
            use_cache=use_cache,
            force_refresh=force_refresh,
        )

    async def bulk_export_to_file(
        self,
        node_id: str,
        output_file: str,
        include_metadata: bool = True,
        use_efficient_traversal: bool = False,
        max_depth: int | None = None,
        child_count_limit: int | None = None,
        max_nodes: int | None = None,
    ) -> dict[str, Any]:
        """Export node tree to hierarchical JSON + Markdown.
        
        Delegates to module-level bulk_export_to_file_impl.
        """
        return await bulk_export_to_file_impl(
            self,
            node_id=node_id,
            output_file=output_file,
            include_metadata=include_metadata,
            use_efficient_traversal=use_efficient_traversal,
            max_depth=max_depth,
            child_count_limit=child_count_limit,
            max_nodes=max_nodes,
        )

    @staticmethod
    def _infer_preview_prefix_from_path(path: str) -> str | None:
        """Infer preview_id prefix from a known NEXUS JSON filename."""
        base = os.path.basename(path)
        if base == "coarse_terrain.json":
            return "CT"
        if base == "phantom_gem.json":
            return "PG"
        if base == "shimmering_terrain.json":
            return "ST"
        if base == "enchanted_terrain.json":
            return "ET"
        return None

    @staticmethod
    def _strip_workflowy_dom_markup_for_tree_preview(
        text: str,
        *,
        unescape_entities: bool = False,
    ) -> str:
        """Strip Workflowy DOM-only tag-pill spam for bracketed tree previews.

        Bracketed/normal GLIMPSE intentionally preserves UUIDs, preview IDs,
        BEACON metadata, SPAN TEXT, and source snippets. What it should NOT
        preserve is Workflowy's generated DOM wrapper noise around clickable
        tags (``<span class="contentTag ...">...</span>``), which makes source
        slices hard to read.

        Formatting policy: preserve simple bold/italic/code tags (``<b>``,
        ``<strong>``, ``<i>``, ``<em>``, ``<code>``). Strip other literal HTML
        tags as DOM presentation wrappers. By default, do NOT unescape entities
        here, so legitimate source text encoded as ``&lt;...&gt;`` remains
        literal and is not turned into live HTML in the bracketed preview.
        """
        if not isinstance(text, str) or not text:
            return ""
        import html as _html
        import re as _re

        s = text
        content_tag_re = _re.compile(
            r"<span\b(?=[^>]*\bclass=[\"'][^\"']*\bcontentTag\b)[^>]*"
            r"\bdata-val=[\"']([^\"']*)[\"'][^>]*>.*?"
            r"<span\b(?=[^>]*\bclass=[\"'][^\"']*\bcontentTagNub\b)[^>]*>\s*</span>\s*</span>",
            _re.IGNORECASE | _re.DOTALL,
        )
        s = content_tag_re.sub(lambda m: _html.unescape(m.group(1)), s)

        allowed_format_tags = {"b", "strong", "i", "em", "code"}

        def _strip_or_preserve_tag(match: Any) -> str:
            raw = match.group(0)
            tag = (match.group(1) or "").lower()
            if tag in allowed_format_tags:
                closing = raw.startswith("</")
                return f"</{tag}>" if closing else f"<{tag}>"
            return ""

        s = _re.sub(r"</?([A-Za-z][\w:-]*)\b[^>]*>", _strip_or_preserve_tag, s)
        return _html.unescape(s) if unescape_entities else s

    @staticmethod
    # @beacon[
    #   id=auto-beacon@WorkFlowyClientEtch._annotate_preview_ids_and_build_tree-k8vf,
    #   role=WorkFlowyClientEtch._annotate_preview_ids_and_build_tree,
    #   slice_labels=glimpse-render,
    #   kind=ast,
    # ]
    def _annotate_preview_ids_and_build_tree(
        nodes: list[dict[str, Any]] | None,
        prefix: str,
        max_note_chars: int | None = None,
        abs_path_by_uuid: dict[str, str] | None = None,
        headings_only: bool = False,
        render_policy: Any = None,
    ) -> list[str]:
        """Assign preview_id to each node and build a human-readable tree.

        Headings-only rows add ▸N total direct children from the source-count
        index on RenderPolicy (completed/hidden children included). ▸? means
        unknown; never use the projected children list as a source count.
        Normal full-body output is unchanged.

        ``abs_path_by_uuid`` (optional): a precomputed {uuid: absolute_path}
        map for Cartographer FILE/FOLDER/ROOT nodes. When supplied, a node
        whose UUID is in the map gets a `` →abs: <path>`` suffix on its
        preview line, surfacing the on-disk location of every mapped
        file/folder/root in FULL (UUID/JSON) mode too (the markdown-mode twin
        already emits a bold abs-path leader). Render-only: the caller builds
        the map from shadow copies; this method only READS it.

        Notes are rendered inline on the same line as the node name.
        By default (max_note_chars=None), notes are **not clipped**; all
        newlines are flattened to literal ``"\\n"`` so each node stays
        on a single line of the preview tree.

        ``render_policy`` (Phase 0.5 Step 7, ap:EXECLDG / v3.1 ① / v3.3 #1):
        an optional :class:`~workflowy_mcp.cache_render_spec.RenderPolicy`
        (duck-typed ``Any`` to avoid an import cycle -- only its
        ``emit_body_false_for`` accessor is called) carrying the
        render-forest-keyed BODY-SUPPRESSION policy. When supplied, the inline
        ``[note]`` suffix is WITHHELD for any occurrence the policy marks --
        INSTEAD OF relying on an upstream note-blanking prune (the v3.1 ①
        hazard-a crack: blanking the note before this serializer ran was both
        the outlawed tree-mutation AND the old full-mode suppression
        mechanism). The serializer now runs over the NOTE-PRESERVING
        render_forest, so ``_build_abs_path_map_for_preview`` can still resolve
        a breadcrumb file node's ``→abs:`` leader from its intact note.

        CRITICAL KEYING (v3.3 #1 -- the load-bearing guardrail): the
        OCCURRENCE-keyed causes are consulted by RENDER-FOREST OCCURRENCE (the
        list-rooted 0-based ``_index_path`` this walk seeds as ``(top_index,)``
        and extends as ``index_path + (ci,)`` -- the SAME coordinate system
        ``_iter_markdown_focus_rows`` / the materializer emit), NOT by bare
        uuid. Under a timestamp sort the same uuid re-emits at multiple
        occurrences and only SOME are breadcrumb ancestors of the focus; a
        uuid-set would suppress the body at EVERY occurrence (re-introducing
        symptom-#5 in full mode). The full/UUID serializer consumes BOTH
        occurrence-keyed causes -- ``breadcrumb`` AND (since U6, ap:E03BN3)
        ``chronological_ancestor`` (every non-leaf of the final forest on a
        chronological render; the inline ``[note]`` of a 25 KB ancestor would
        leak identically here otherwise) -- through this ONE
        ``include_deprecated=False`` consult: only the uuid-keyed DEPBOD
        surviving-``--deprecated`` suppression stays Markdown-only in Phase 0.5
        (v3.1 ② / v3.2(a)) unless separately signed off. Default None => no
        policy consulted, byte-for-byte prior behavior.

        IMMUTABILITY (v3.3 #3): ``preview_id`` is assigned into a serializer-
        LOCAL map keyed by the node's positional path, NEVER written onto the
        input node dicts -- so running this over a MaterializedRenderDocument's
        render_forest leaves the stored forest unmutated (the ``source forest
        never mutated`` invariant holds for the full/UUID path too). No
        external caller ever read the old ``node['preview_id']`` side effect
        (verified), so this is output-identical.
        """
        if not nodes:
            return []

        # PASS 1: Assign preview_id to all nodes and collect max width.
        # v3.3 #3: preview_id lives in a LOCAL map keyed by the node's 1-based
        # positional path (unique per position), NEVER written onto the node
        # dict -- so the materialized render_forest is never mutated.
        all_preview_ids: list[str] = []
        preview_id_by_path: dict[tuple[str, ...], str] = {}

        def assign_ids(node: dict[str, Any], path_parts: list[str]) -> None:
            if not isinstance(node, dict):
                return
            path_str = ".".join(path_parts)
            preview_id = f"{prefix}-{path_str}"
            preview_id_by_path[tuple(path_parts)] = preview_id
            all_preview_ids.append(preview_id)
            children = node.get("children") or []
            for idx, child in enumerate(children, start=1):
                if isinstance(child, dict):
                    assign_ids(child, path_parts + [str(idx)])

        for root_index, root in enumerate(nodes, start=1):
            if isinstance(root, dict):
                assign_ids(root, [str(root_index)])

        # Compute max width for right-padding
        max_id_width = max((len(pid) for pid in all_preview_ids), default=0)

        # PASS 2: Build aligned preview lines.
        # ``index_path`` is the RENDER-FOREST 0-based list-rooted route (the
        # SAME coordinate the RenderPolicy breadcrumb set is keyed by); it runs
        # in lockstep with the 1-based ``path_parts`` used for the preview_id.
        lines: list[str] = []

        def walk(
            node: dict[str, Any],
            path_parts: list[str],
            index_path: tuple[int, ...],
        ) -> None:
            if not isinstance(node, dict):
                return
            preview_id = preview_id_by_path.get(tuple(path_parts), "")
            padded_id = preview_id.ljust(max_id_width)

            # Build one-line preview
            depth = max(1, len(path_parts))
            indent = " " * 4 * (depth - 1)
            children = node.get("children") or []
            has_child_dicts = any(isinstance(c, dict) for c in children)
            bullet = "•" if not has_child_dicts else "⦿"
            name = node.get("name") or "Untitled"
            name = WorkFlowyClientEtch._strip_workflowy_dom_markup_for_tree_preview(str(name))
            note = node.get("note") or ""
            node_uuid = node.get("id") or node.get("uuid") or ""
            if headings_only:
                from ..node_child_counts import format_child_count_suffix

                name += format_child_count_suffix(
                    str(node_uuid), getattr(render_policy, "direct_child_counts", None)
                )
            # BODY-SUPPRESSION POLICY (Phase 0.5 Step 7): consult the render-
            # forest OCCURRENCE-keyed causes -- breadcrumb AND (U6, ap:E03BN3)
            # chronological-ancestor. include_deprecated=
            # False keeps DEPBOD Markdown-only (v3.1 ② / v3.2(a)). When the
            # policy withholds this occurrence's body, we render name-only --
            # exactly as if the note were absent, but WITHOUT ever blanking the
            # note (so the abs-path map, built over the intact note, still
            # resolves the file-node leader -- the v3.1 ① hazard-a fix).
            _emit_body_false = False
            if render_policy is not None:
                try:
                    _emit_body_false = bool(
                        render_policy.emit_body_false_for(
                            render_key=index_path,
                            uuid=str(node_uuid),
                            include_deprecated=False,
                        )
                    )
                except Exception:
                    _emit_body_false = False
            # HEADINGS-ONLY (full/UUID mode twin of the markdown-mode flag):
            # drop the inline ``[note]`` suffix entirely, keeping only the node
            # name (plus UUID / preview_id / abs-path). The token-slashing
            # "titles only" tree view -- structure without body content.
            if headings_only or _emit_body_false:
                name_part = name
            elif isinstance(note, str) and note:
                note = WorkFlowyClientEtch._strip_workflowy_dom_markup_for_tree_preview(note)
                flat = note.replace("\n", "\\n")
                if isinstance(max_note_chars, int) and max_note_chars > 0 and len(flat) > max_note_chars:
                    flat = flat[:max_note_chars]
                name_part = f"{name} [{flat}]"
            else:
                name_part = name

            # Include Workflowy UUID first, then local preview_id for cross-referencing
            uuid_label = str(node_uuid)
            # FULL-mode abs-path surfacing (Task C): mapped file/folder/root
            # nodes get their resolved absolute on-disk path appended so an
            # agent never has to reconstruct it from the heading/UUID chain.
            abs_suffix = ""
            if abs_path_by_uuid:
                _ap = abs_path_by_uuid.get(str(node_uuid))
                if _ap:
                    abs_suffix = f" \u2192abs: {_ap}"
            lines.append(f"[{uuid_label}] [{padded_id}] {indent}{bullet} {name_part}{abs_suffix}")

            # Recurse into children with extended path (1-based path_parts for
            # the preview_id; 0-based index_path for the policy key).
            for idx, child in enumerate(children, start=1):
                if isinstance(child, dict):
                    walk(child, path_parts + [str(idx)], index_path + (idx - 1,))

        for root_index, root in enumerate(nodes, start=1):
            if isinstance(root, dict):
                walk(root, [str(root_index)], (root_index - 1,))

        return lines

    def _build_hierarchy(
        self,
        flat_nodes: list[dict[str, Any]],
        include_metadata: bool = True
    ) -> list[dict[str, Any]]:
        """Convert flat node list to hierarchical tree structure."""
        # Pass 1: Build lookup dictionary and add children arrays
        nodes_by_id: dict[str, dict[str, Any]] = {}
        for node in flat_nodes:
            node_copy = node.copy()
            
            # Strip metadata if requested
            if not include_metadata:
                node_copy.pop('created_at', None)
                node_copy.pop('modified_at', None)
                node_copy.pop('createdAt', None)
                node_copy.pop('modifiedAt', None)
            
            node_copy['children'] = []
            nodes_by_id[node['id']] = node_copy
        
        # Pass 2: Link children to parents
        root_nodes = []
        for node in nodes_by_id.values():
            parent_id = node.get('parent_id') or node.get('parentId')
            if parent_id and parent_id in nodes_by_id:
                nodes_by_id[parent_id]['children'].append(node)
            else:
                # No parent or parent not in set = root node
                root_nodes.append(node)

        # Pass 3: Sort each sibling group by Workflowy 'priority' field
        def _sort_children_by_priority(nodes_list: list[dict[str, Any]]) -> None:
            for n in nodes_list:
                children = n.get('children') or []
                if children:
                    children.sort(key=lambda c: c.get('priority', 0))
                    _sort_children_by_priority(children)

        _sort_children_by_priority(root_nodes)

        return root_nodes
    
    def _calculate_max_depth(self, nodes: list[dict[str, Any]], current_depth: int = 1) -> int:
        """Calculate maximum depth of node tree."""
        if not nodes:
            return current_depth - 1
        
        max_child_depth = current_depth
        for node in nodes:
            if node.get('children'):
                child_depth = self._calculate_max_depth(node['children'], current_depth + 1)
                max_child_depth = max(max_child_depth, child_depth)
        
        return max_child_depth
    
    def _limit_depth(self, nodes: list[dict[str, Any]], max_depth: int, current_depth: int = 1) -> list[dict[str, Any]]:
        """Limit node tree to specified depth."""
        if not nodes or current_depth > max_depth:
            return []
        
        limited_nodes = []
        for node in nodes:
            node_copy = node.copy()
            
            if current_depth < max_depth and node.get('children'):
                # Recursively limit children
                node_copy['children'] = self._limit_depth(node['children'], max_depth, current_depth + 1)
            else:
                # At max depth - truncate children
                node_copy['children'] = []
            
            limited_nodes.append(node_copy)
        
        return limited_nodes
    
    def _annotate_child_counts_and_truncate(
        self,
        nodes: list[dict[str, Any]],
        max_depth: int | None = None,
        child_count_limit: int | None = None,
        current_depth: int = 1,
    ) -> int:
        """Annotate child/descendant counts and optionally truncate children."""
        if not nodes:
            return 0

        total_descendants_here = 0

        for node in nodes:
            children = node.get('children') or []

            # First, recursively annotate children
            child_desc_total = 0
            if children:
                child_desc_total = self._annotate_child_counts_and_truncate(
                    children,
                    max_depth=max_depth,
                    child_count_limit=child_count_limit,
                    current_depth=current_depth + 1,
                )

            immediate_count = len(children)
            total_desc_for_node = child_desc_total + immediate_count

            node['immediate_child_count__human_readable_only'] = immediate_count
            node['total_descendant_count__human_readable_only'] = total_desc_for_node

            # Decide truncation
            status = 'complete'
            truncate_children = False

            if max_depth is not None and current_depth >= max_depth and immediate_count > 0:
                status = 'truncated_by_depth'
                truncate_children = True

            if (
                not truncate_children
                and child_count_limit is not None
                and immediate_count > child_count_limit
            ):
                status = 'truncated_by_count'
                truncate_children = True

            if truncate_children:
                node['children'] = []

            node['children_status'] = status
            total_descendants_here += total_desc_for_node

        return total_descendants_here

    def _generate_markdown(
        self,
        nodes: list[dict[str, Any]],
        level: int = 1,
        parent_path_uuids: list[str] | None = None,
        parent_path_names: list[str] | None = None,
        root_node_info: dict[str, Any] | None = None
    ) -> str:
        """Convert hierarchical nodes to Markdown format with UUID metadata."""
        if parent_path_uuids is None:
            parent_path_uuids = []
        if parent_path_names is None:
            parent_path_names = []
            
        markdown_lines = []

        def append_note(note: str | None) -> None:
            # The SAME note serialization for the exported root and children;
            # preserve the existing fenced-body presentation and raw content.
            if note and note.strip():
                is_markdown = any(line.strip().startswith('#') for line in note.split('\n'))
                language = 'markdown' if is_markdown else 'text'
                markdown_lines.append('````````````' + language)
                markdown_lines.append(note)
                markdown_lines.append('````````````')
                markdown_lines.append('')

        # Add root metadata at top of file (level 1 only)
        if level == 1 and root_node_info:
            root_uuid = root_node_info.get('id', '')
            root_name = root_node_info.get('name', 'Root')
            full_path_uuids = root_node_info.get('full_path_uuids', [root_uuid])
            full_path_names = root_node_info.get('full_path_names', [root_name])
            
            # Truncated UUID path for readability
            truncated_path = ' > '.join([uuid[:8] + '...' for uuid in full_path_uuids])
            # Full name path for orientation
            name_path = ' > '.join(full_path_names)
            
            markdown_lines.append(f'<!-- EXPORTED_ROOT_UUID: {root_uuid} -->')
            markdown_lines.append(f'<!-- EXPORTED_ROOT_NAME: {root_name} -->')
            markdown_lines.append(f'<!-- EXPORTED_ROOT_PATH_UUIDS: {truncated_path} -->')
            markdown_lines.append(f'<!-- EXPORTED_ROOT_PATH_NAMES: {name_path} -->')
            markdown_lines.append('')
            # Root notes used to vanish when the exporter peeled the root off
            # the children array. Emit it here once, even for a childless root;
            # child heading levels, paths and preview numbering stay unchanged.
            append_note(root_node_info.get('note'))
        
        for node in nodes:
            node_uuid = node.get('id', '')
            node_name = node.get('name', 'Untitled')
            
            # Build paths for this node
            current_path_uuids = parent_path_uuids + [node_uuid]
            current_path_names = parent_path_names + [node_name]
            
            # Create heading (limit to h6)
            heading_level = min(level, 6)
            heading = '#' * heading_level + ' ' + node_name
            markdown_lines.append(heading)
            
            # Add metadata (hidden in Obsidian reading view)
            markdown_lines.append(f'<!-- NODE_UUID: {node_uuid} -->')
            
            # Truncated UUID path
            truncated_uuids = [uuid[:8] + '...' for uuid in current_path_uuids]
            uuid_path = ' > '.join(truncated_uuids)
            markdown_lines.append(f'<!-- NODE_PATH_UUIDS: {uuid_path} -->')
            
            # Name path
            name_path = ' > '.join(current_path_names)
            markdown_lines.append(f'<!-- NODE_PATH_NAMES: {name_path} -->')
            
            markdown_lines.append('')
            
            # Add note content through the same root/child body serializer.
            append_note(node.get('note'))

            # Recursively process children
            children = node.get('children', [])
            if children:
                child_markdown = self._generate_markdown(
                    children, 
                    level + 1,
                    current_path_uuids,
                    current_path_names
                )
                markdown_lines.append(child_markdown)
            
        return '\n'.join(markdown_lines)
    
    def generate_markdown_from_json(self, json_file: str) -> dict[str, Any]:
        """Convert JSON file to Markdown (without metadata - for edited JSON review)."""
        try:
            # Read JSON file
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Handle both bare array AND metadata wrapper
            if isinstance(data, dict) and "nodes" in data:
                nodes = data["nodes"]
            elif isinstance(data, list):
                nodes = data
            else:
                return {
                    "success": False,
                    "markdown_file": None,
                    "error": f"JSON must be array or dict with 'nodes' key. Got: {type(data).__name__}"
                }
            
            # Generate Markdown WITH metadata
            markdown_content = self._generate_markdown(nodes, level=1)
            
            # Write to .md file
            markdown_file = json_file.replace('.json', '.md')
            with open(markdown_file, 'w', encoding='utf-8') as f:
                f.write(markdown_content)
            
            return {
                "success": True,
                "markdown_file": markdown_file
            }
            
        except Exception as e:
            return {
                "success": False,
                "markdown_file": None,
                "error": f"Failed to generate markdown: {str(e)}"
            }

    # @beacon[
    #   id=core-crud@workflowy_etch,
    #   role=WorkFlowyClientEtch.workflowy_etch,
    #   slice_labels=nexus-core-v1,node-ops,
    #   kind=ast,
    #   comment=Append-only validated forest creation; optional server-supplied fresh_root_hold surrounds recursion below each acknowledged top-level root with children; default None preserves existing client behavior and no CRUD funnel acquires a region; the receipt carries root_node_ids on the success path AND the outer catch, so a late failure still names the roots that landed.,
    # ]
    async def workflowy_etch(
        self,
        parent_id: str,
        nodes: list[dict[str, Any]] | str,
        nodes_file: str | None = None,
        *,
        fresh_root_hold=None,
    ) -> dict[str, Any]:
        """Create multiple nodes from JSON structure (ETCH command).

        Args:
            parent_id: Target Workflowy parent id.
            nodes: Either a parsed list[dict] structure or a JSON string
                representing that list. Ignored when nodes_file is provided.
            nodes_file: Optional path to a file containing ETCH payload.
                - If the file contains a JSON object with a top-level
                  "nodes" key, that value is used.
                - Else, the entire file content is treated as a JSON
                  string to be parsed the same way as the "nodes" string
                   parameter (including autofix strategies).
            fresh_root_hold: Optional async context-manager factory taking a
                freshly created top-level root UUID. Enter it before its first
                child create and exit after that root's recursion on every path.
                Leaf roots need none; None preserves the existing client path.

        Returns:
            The receipt dict. ``root_node_ids`` (the top-level roots actually
            created) is present on BOTH paths -- the success path and the outer
            catch -- so a failure raised after the forest was created still
            tells the caller which roots landed.
        """
        import asyncio

        logger = _ClientLogger()

        # Optional file-based payload: if nodes_file is provided, it wins
        # over the inline "nodes" argument. The read routes through the ONE
        # host-aware read shape (tool_path_resolution.read_tool_text_file);
        # a ToolPathError lands in the established error dict below (§6.7).
        if nodes_file:
            try:
                from ..tool_path_resolution import read_tool_text_file

                _nodes_res, file_text = read_tool_text_file(
                    nodes_file, param="nodes_file", context="workflowy_etch"
                )
                # Try JSON object with top-level "nodes" first
                try:
                    maybe_json = json.loads(file_text)
                    if isinstance(maybe_json, dict) and "nodes" in maybe_json:
                        nodes = maybe_json["nodes"]
                    else:
                        # Not a dict-with-nodes; treat entire file_text as
                        # the stringified nodes payload
                        nodes = file_text
                    logger.info(
                        f"📄 workflowy_etch: loaded nodes from file '{nodes_file}'"
                    )
                except json.JSONDecodeError:
                    # Not valid JSON as a whole; assume raw JSON string
                    # payload for nodes and let the normal string handler
                    # below deal with it (including autofix).
                    nodes = file_text
                    logger.info(
                        f"📄 workflowy_etch: using raw file text from '{nodes_file}'"
                    )
            except Exception as e:
                error_msg = (
                    f"Failed to read nodes_file '{nodes_file}': {e}"
                )
                self._log_to_file(error_msg, "etch")
                return {
                    "success": False,
                    "nodes_created": 0,
                    "root_node_ids": [],
                    "errors": [error_msg],
                }
        
        # Auto-fix stringified JSON via the ONE SHARED parser
        # (parse_etch_nodes_payload) -- the SAME function add_to_anchor's
        # forest-structure peek uses, so the two can never diverge on what a
        # given input parses to. Strategy 1 (direct json.loads) + Strategy 2
        # (structure-aware trim after the last ']') live in that function now,
        # not inline here.
        stringify_strategy_used = None
        if isinstance(nodes, str):
            logger.warning("⚠️ Received stringified JSON - attempting parse strategies")
            _orig_len = len(nodes)
            parsed, stringify_strategy_used, parse_error = parse_etch_nodes_payload(nodes)
            if parse_error:
                self._log_to_file(parse_error, "etch")
                return {
                    "success": False,
                    "nodes_created": 0,
                    "root_node_ids": [],
                    "errors": [parse_error],
                }
            nodes = parsed
            if stringify_strategy_used and "Strategy 2" in stringify_strategy_used:
                self._log_to_file(
                    "STRINGIFY AUTOFIX: Strategy 2 used. "
                    f"Original len={_orig_len}.",
                    "etch",
                )
            if stringify_strategy_used:
                logger.info(f"✅ {stringify_strategy_used}")

        # Validate nodes is a list
        if not isinstance(nodes, list):
            return {
                "success": False,
                "nodes_created": 0,
                "root_node_ids": [],
                "errors": ["Parameter 'nodes' must be a list"]
            }
        
        # Pure validation pass over the WHOLE forest, BEFORE any create call.
        # This is the ONE ETCH-forest grammar authority (the same seam every
        # ETCH ingress reaches: node.etch sync/async, anchor.point.capture's
        # `nodes` form, and nodes_file), so unknown-key rejection lives here
        # and nowhere else -- the same one-normalizer-per-request-kind doctrine
        # as LightningRodMutationSpec / _normalize_operations / parse_lens_spec.
        # Because it runs to completion before create_tree starts, a malformed
        # node ANYWHERE in the forest means ZERO writes.
        #
        # IMPORTANT: NO encoding here. Encoding happens exactly once inside
        # create_node when each child is created below. ETCH used to also
        # encode here (and create_node encoded again, accidentally producing
        # double-encoded NAME bytes that ETCH relied on for rendering).
        # That accidental double-pass is now an explicit single-pass
        # double-encode inside encode_for_workflowy_api(field='name'), so
        # ETCH must NOT pre-encode.
        def validate_and_escape_nodes_recursive(
            nodes_list: list[dict[str, Any]], path: str = "nodes"
        ) -> tuple[bool, str | None, list[str]]:
            """Recursively validate every node's shape and fields. No encoding."""
            warnings: list[str] = []
            for idx, node in enumerate(nodes_list):
                node_path = f"{path}[{idx}]"

                if not isinstance(node, dict):
                    return (
                        False,
                        f"{node_path} must be an object "
                        f"{ETCH_NODE_GRAMMAR}; got {type(node).__name__}.",
                        warnings,
                    )

                unknown = sorted(set(node) - ETCH_NODE_ALLOWED_KEYS)
                if unknown:
                    return (
                        False,
                        _etch_unknown_keys_message(node_path, unknown),
                        warnings,
                    )

                # `name` must be PRESENT and non-null here. _validate_name(None)
                # deliberately returns OK for other callers (it means 'no name
                # change'), but an ETCH node with name=null would pass preflight
                # and then fail per-node in create_tree AFTER earlier siblings
                # were created -- exactly the partial-write this preflight exists
                # to prevent.
                name = node.get('name')
                if name is None:
                    return (
                        False,
                        f"{node_path}.name is required (every ETCH node needs a "
                        f"non-empty string name; null is not allowed). "
                        f"Node grammar: {ETCH_NODE_GRAMMAR}.",
                        warnings,
                    )
                name_ok, name_err = self._validate_name(name)
                if not name_ok:
                    return (False, f"{node_path}.name: {name_err}", warnings)

                note = node.get('note')
                if note is not None:
                    note_ok, note_err = self._validate_note(
                        note,
                        skip_newline_check=False,
                    )
                    if not note_ok:
                        return (False, f"{node_path}.note: {note_err}", warnings)

                # Enum fields: type-check BEFORE set membership (an unhashable
                # list/dict value would otherwise raise TypeError out of the
                # preflight instead of producing a path-bearing error).
                layout_mode = node.get('layout_mode')
                if layout_mode is not None and (
                    not isinstance(layout_mode, str) or layout_mode not in ETCH_LAYOUT_MODES
                ):
                    return (
                        False,
                        f"{node_path}.layout_mode must be one of "
                        f"{sorted(ETCH_LAYOUT_MODES)} or omitted; got {layout_mode!r}.",
                        warnings,
                    )

                position = node.get('position')
                if position is not None and (
                    not isinstance(position, str) or position not in ETCH_POSITIONS
                ):
                    return (
                        False,
                        f"{node_path}.position must be one of "
                        f"{sorted(ETCH_POSITIONS)} or omitted; got {position!r}.",
                        warnings,
                    )

                children = node.get('children')
                if children is not None and not isinstance(children, list):
                    return (
                        False,
                        f"{node_path}.children must be a list of node objects "
                        f"(or omitted); got {type(children).__name__}.",
                        warnings,
                    )
                if children:
                    success, error_msg, child_warnings = (
                        validate_and_escape_nodes_recursive(
                            children, f"{node_path}.children"
                        )
                    )
                    if not success:
                        return (False, error_msg, warnings)
                    warnings.extend(child_warnings)

            return (True, None, warnings)
        
        success, error_msg, warnings = validate_and_escape_nodes_recursive(nodes)
        
        if not success:
            return {
                "success": False,
                "nodes_created": 0,
                "root_node_ids": [],
                "errors": [error_msg or "Validation failed"]
            }
        
        if warnings:
            logger.info(f"✅ Auto-escaped {len(warnings)} node(s)")
        
        # Stats tracking
        stats = {
            "api_calls": 0,
            "retries": 0,
            "rate_limit_hits": 0,
            "nodes_created": 0,
            "errors": []
        }

        # ETCH is append-only. Historical replacement support was
        # destructive and surprising; it has been removed entirely. If a
        # future destructive operation is needed, expose it as a separate,
        # explicitly named tool rather than overloading ETCH.
        nodes_to_create = nodes

        # Create tree recursively
        async def create_tree(parent_id: str, nodes: list[dict[str, Any]], *, top_level=True) -> list[str]:
            """Create the tree; the optional hook protects only fresh-root recursion."""
            created_ids = []
            
            for node_data in nodes:
                try:
                    node_name = node_data['name']
                    
                    request = NodeCreateRequest(
                        name=node_name,
                        parent_id=parent_id,
                        note=node_data.get('note'),
                        layoutMode=node_data.get('layout_mode'),
                        position=node_data.get('position', 'bottom')
                    )
                    
                    # Create with retry (internal call)
                    node = await self.create_node(request, _internal_call=True)
                    
                    if node:
                        created_ids.append(node.id)
                        stats["nodes_created"] += 1
                        self._log_to_file(f"  Created: {node_name} ({node.id})", "etch")
                        
                        # Recursively create children
                        if 'children' in node_data and node_data['children']:
                            if top_level and fresh_root_hold is not None:
                                async with fresh_root_hold(node.id):
                                    await create_tree(node.id, node_data['children'], top_level=False)
                            else:
                                await create_tree(node.id, node_data['children'], top_level=False)
                
                except Exception as e:
                    error_msg = f"Failed to create '{node_data.get('name', 'unknown')}': {str(e)}"
                    logger.error(error_msg)
                    stats["errors"].append(error_msg)
                    continue
            
            return created_ids
        
        # Bound BEFORE the outer try: that catch also handles a failure raised
        # AFTER create_tree returned (the completion log, typically), when the
        # roots below have already landed on the server. Initialising here is
        # what lets the catch report them instead of omitting root_node_ids (or
        # masking the original error with an UnboundLocalError).
        root_ids: list[str] = []

        try:
            self._log_to_file(f"ETCH start (append-only) parent={parent_id}", "etch")
            root_ids = await create_tree(parent_id, nodes_to_create)
            
            self._log_to_file(f"ETCH complete: {stats['nodes_created']} created", "etch")
            
            result = {
                "success": len(stats["errors"]) == 0,
                "nodes_created": stats["nodes_created"],
                "root_node_ids": root_ids,
                "api_calls": stats["api_calls"],
                "retries": stats["retries"],
                "errors": stats["errors"]
            }
            
            if stringify_strategy_used:
                result["_stringify_autofix"] = stringify_strategy_used

            # Mark parent dirty
            if result.get("success", False):
                try:
                    self._mark_nodes_export_dirty([parent_id])
                except Exception:
                    pass
            
            return result
            
        except Exception as e:
            error_msg = f"ETCH failed: {str(e)}"
            log_event(error_msg, "ETCH")
            stats["errors"].append(error_msg)
            
            # Same receipt vocabulary as the success path: a raise after
            # create_tree returned leaves real roots on the server, so the
            # failure receipt names them ([] when the raise preceded any
            # create) instead of dropping the field.
            return {
                "success": False,
                "nodes_created": stats["nodes_created"],
                "root_node_ids": root_ids,
                "errors": stats["errors"]
            }


# @beacon[
#   id=coherence@fetch-nodes-export-uncached,
#   slice_labels=ra-workflowy-cache,carto-governance,mci-epoch-seams,
#   kind=ast,
#   comment=S02 H3(a) single fetch path: the ONLY production code that issues/retries/validates an explicit /nodes-export network fetch; returns the RAW wire payload — no cache assignment, no dirty-ID clearing, no legacy snapshot write, no dewhitening (the coherence authority canonicalizes exactly once through FULL_EXPORT_REPLACE).,
# ]
async def fetch_nodes_export_uncached(
    client: WorkFlowyClientCore,
    *,
    max_retries: int = 10,
) -> dict[str, Any]:
    """Explicit full /nodes-export fetch with pacing and bounded retries.

    S02 seam (spec §7.3): returns the RAW private payload for the coherence
    refresh authority.  It does NOT set cache fields, does NOT clear dirty
    IDs, does NOT write a legacy snapshot, and does NOT dewhiten — the
    authority canonicalizes exactly once through the S01 FULL_EXPORT_REPLACE
    codec.  Returns only after complete response/shape validation; on
    failure, previous process-local and shared state remain untouched.
    """

    logger = _ClientLogger()
    retry_count = 0
    base_delay = 1.0

    while retry_count < max_retries:
        await client._sleep_with_operation_cancel(
            API_RATE_LIMIT_DELAY,
            "Workflowy /nodes-export pacing",
        )

        try:
            response = await client.client.get("/nodes-export")
            data = await client._handle_response(response)

            all_nodes = data.get("nodes", []) or []
            if not isinstance(all_nodes, list):
                raise NetworkError("/nodes-export payload is missing its 'nodes' list")
            data["_total_fetched_from_api"] = len(all_nodes)

            if retry_count > 0:
                success_msg = (
                    f"export_nodes succeeded after {retry_count + 1}/{max_retries} attempts"
                )
                logger.info(success_msg)
                _log_to_file_helper(success_msg, "reconcile")

            return data

        except Exception as e:
            retry_count += 1

            # Make rate-limit waits explicit in logs so F12/WEAVE behavior is
            # easier to interpret when /nodes-export is throttled.
            if isinstance(e, RateLimitError):
                retry_after_hint = None
                try:
                    # RateLimitError.details may carry a "retry_after" hint.
                    retry_after_hint = getattr(e, "details", {}).get("retry_after")
                except Exception:
                    retry_after_hint = None

                retry_delay = base_delay * (2**retry_count)
                msg = (
                    "export_nodes_impl: Rate limited on /nodes-export. "
                    f"Retry {retry_count}/{max_retries} after {retry_delay:.1f}s "
                    f"(retry_after={retry_after_hint!r})"
                )
                logger.warning(msg)
                _log_to_file_helper(msg, "reconcile")
            else:
                logger.warning(f"Export error: {e}. Retry {retry_count}/{max_retries}")

            if retry_count < max_retries:
                await client._sleep_with_operation_cancel(
                    base_delay * (2**retry_count),
                    "Workflowy /nodes-export retry",
                )
            else:
                raise

    raise NetworkError("export_nodes failed after maximum retries")


# @beacon[
#   id=auto-beacon@export_nodes_impl-bweo,
#   slice_labels=ra-workflowy-cache,mci-cache-seams,
#   kind=ast,
#   comment=Keeps S03 pinned-view dirty checks and the explicit S02 refresh authority on-loop then joins the data-only cached export projection off-loop; row aliases shallow wrappers parent-key traversal and loud boundary/unpopulated errors remain unchanged with no worker cache or client transfer.,
# ]
async def export_nodes_impl(
    client: WorkFlowyClientCore,
    node_id: str | None = None,
    max_retries: int = 10,
    use_cache: bool = True,
    force_refresh: bool = False,
    _allow_api_refresh: bool = False,
) -> dict[str, Any]:
    """Export all nodes or filter to specific node's subtree (implementation).

    This is extracted as a module-level function to avoid circular imports.

    _allow_api_refresh is an internal guard: only explicit cache-refresh
    operations (triggered by Dan via the UUID widget) are permitted to
    call /nodes-export. All other callers must rely on the existing
    in-memory snapshot and will fail fast if no cache is present.

    S02: the refresh branch delegates to the coherence refresh authority via
    ``client.refresh_nodes_export_cache`` (H1/H3-a) — no independent
    download/retry body and no eager pre-publication cache assignment
    remains here.

    S03 §9 (alias + one-generation contract): on a ``use_cache`` hit this
    consumes the current task-local :class:`OperationCacheSnapshot` — the
    operation's PINNED immutable view payload plus its welded dirty
    bookkeeping (B1.1) — never a mutable publisher-owned global read at two
    separate instants.  A caller outside an operation boundary is a migration
    bug: with a populated publisher it fails LOUDLY with ``CacheViewError``
    (S03 closure, CF-S03CR-01 — the pre-S03 boundary-less property fallback
    is retired); an unpopulated cache keeps the actionable ``NetworkError``
    refresh-required dialect.
    """
    logger = _ClientLogger()

    from ..coherence.cache_view import is_unpopulated_view
    from ..coherence.errors import CacheViewError
    from ..coherence.operation_cache import current_operation_cache

    _snapshot = current_operation_cache(required=False)
    _snapshot_view = None
    _snapshot_dirty: frozenset = frozenset()
    if (
        _snapshot is not None
        and _snapshot.owner is (getattr(client, "_cache_view_publisher", None) or client)
        and not is_unpopulated_view(_snapshot.view)
    ):
        _snapshot_view = _snapshot.view
        _snapshot_dirty = _snapshot.dirty_ids

    # Decide cache vs fetch
    if (not use_cache) or force_refresh or (
        _snapshot_view is None and client._nodes_export_cache is None
    ):
        if not _allow_api_refresh:
            # No automatic cache refresh; require explicit refresh via widget.
            raise NetworkError(
                "nodes-export cache is not initialized; run "
                "workflowy_refresh_nodes_export_cache from the UUID widget."
            )
        await client.refresh_nodes_export_cache(max_retries=max_retries)
        data = client._nodes_export_cache
        if not isinstance(data, dict):
            raise NetworkError("refresh authority completed without an adopted cache")
    elif _snapshot_view is not None:
        data = _snapshot_view.nodes_export

        # Check dirty IDs for subtree requests (welded to the pinned view, B1.1)
        if node_id is not None and _snapshot_dirty:
            nodes_by_id = _snapshot_view.nodes_by_id

            if node_id not in nodes_by_id:
                # In manual mode, this indicates a stale snapshot; surface a
                # clear error instead of silently triggering a fresh export.
                logger.warning(
                    "export_nodes_impl: node %s not present in cached /nodes-export; "
                    "automatic refresh is disabled", node_id,
                )
                raise NetworkError(
                    "nodes-export cache does not include requested node; run "
                    "workflowy_refresh_nodes_export_cache from the UUID widget."
                )
            else:
                dirty = _snapshot_dirty
                path_hits_dirty = False
                cur = node_id
                visited: set[str] = set()

                while cur and cur not in visited:
                    visited.add(cur)
                    if cur in dirty or "*" in dirty:
                        path_hits_dirty = True
                        break
                    parent_id = (
                        nodes_by_id[cur].get("parent_id")
                        or nodes_by_id[cur].get("parentId")
                    )
                    cur = parent_id

                if path_hits_dirty:
                    # Honor Dan's preference: do NOT trigger API refresh here; just log.
                    logger.info(
                        "export_nodes_impl: path from %s hits dirty; using existing "
                        "nodes-export cache (automatic refresh disabled)",
                        node_id,
                    )
                else:
                    logger.info(f"Using cached export for {node_id}")
    else:
        # S03 closure (CF-S03CR-01): a POPULATED publisher with no valid
        # same-authority operation snapshot is a migration bug — fail LOUDLY,
        # never silently serve the publisher's CURRENT view with zero journal
        # adoption.  The pre-S03 boundary-less property fallback (and its
        # private dirty-walk duplicate) is retired; under a hypothetical
        # legacy profile every cache-backed legacy front now fails closed
        # here or at the §6.4 loud accessor.
        raise CacheViewError(
            "export_nodes_impl: no operation cache snapshot is active in "
            "this task (cache-backed code must run inside a "
            "cache_operation_boundary)"
        )

    return await run_carto_cpu("export:project", _project_cached_export, data, node_id)


# @beacon[
#   id=carto-runtime@project-cached-export,
#   slice_labels=ra-carto-jobs,ra-workflowy-cache,
#   kind=ast,
#   comment=Cached export filtering and stack walk on a borrowed immutable payload without the retired diagnostic scan; returns the same row aliases and shallow count wrappers without a client implicit operation lookup or cache publication.,
# ]
def _project_cached_export(data, node_id):
    """Pure data projection without diagnostics; snapshot/dirty checks are caller-owned."""
    # Filter if needed
    all_nodes = data.get("nodes", []) or []

    total_before_filter = len(all_nodes)

    if node_id is None:
        if "_total_fetched_from_api" not in data:
            # S03 (S02 ledger item 8): NEVER patch the key into the shared
            # cache payload in place — the data IS an immutable published
            # view now (Axiom 5).  Return a shallow wrapper carrying the
            # count instead; the view itself is untouched.
            data = {**data, "_total_fetched_from_api": total_before_filter}
        return data

    # Filter to subtree
    included_ids = {node_id}
    nodes_by_id = {node["id"]: node for node in all_nodes if node.get("id")}

    # One-pass parent-to-children index + an explicit stack walk: the same
    # id set and the same inclusion guard as the retired per-node rescan
    # (one full scan of all_nodes per included node), in ONE scan total.
    # IMPORTANT: /nodes-export historically used `parent_id` (snake_case) in
    # its payload, but our in-memory cache may also contain nodes that were
    # appended via single-node operations (create_node, etc.) whose parent
    # linkage lives in `parentId` (camelCase). To avoid losing newly-created
    # children in subtree exports, the index honors BOTH keys here.
    children_by_parent: dict[Any, list[Any]] = {}
    for node in all_nodes:
        nid = node.get("id")
        if not nid:
            continue
        pid = node.get("parent_id") or node.get("parentId")
        children_by_parent.setdefault(pid, []).append(nid)

    if node_id in nodes_by_id:
        stack = [node_id]
        while stack:
            current = stack.pop()
            for nid in children_by_parent.get(current, ()):
                if nid not in included_ids:
                    included_ids.add(nid)
                    stack.append(str(nid))

    filtered_nodes = [node for node in all_nodes if node["id"] in included_ids]

    return {
        "nodes": filtered_nodes,
        "_total_fetched_from_api": data.get("_total_fetched_from_api", total_before_filter),
        "_filtered_count": len(filtered_nodes),
    }


# @beacon[
#   id=scry@bulk-export-root-note,
#   slice_labels=node-ops,
#   kind=ast,
#   comment=Existing SCRY file-export authority: preserves the children-only terrain layout and preview identities while carrying the root note as export_root_note and in the companion Markdown. Root content comes from the same exported subtree, never an extra live read; existing advisory and backup handling are unchanged.,
# ]
async def bulk_export_to_file_impl(
    client: WorkFlowyClientCore,
    node_id: str,
    output_file: str,
    include_metadata: bool = True,
    use_efficient_traversal: bool = False,
    max_depth: int | None = None,
    child_count_limit: int | None = None,
    max_nodes: int | None = None,
) -> dict[str, Any]:
    """Export node tree to hierarchical JSON + Markdown (implementation)."""
    # Full implementation from original
    # [Lines ~800-1100 from original api_client.py]
    # Delegated to standalone function to avoid circular imports

    # Host-aware resolution FIRST — before the fetch (P1: a refused scry
    # path fails with zero API calls and zero bytes written anywhere) and
    # before every derived companion path is computed (§13 row 7). The
    # ToolPathError propagates as today's client-layer errors do (§6.7).
    from ..tool_path_resolution import resolve_tool_path

    path_resolution = resolve_tool_path(
        output_file, param="output_file", context="node.scry", direction="write"
    )
    resolved_file = path_resolution.resolved

    # Efficient traversal or full export
    total_nodes_fetched = 0
    api_calls_made = 0
    
    if use_efficient_traversal:
        from collections import deque
        flat_nodes = []
        queue = deque([node_id])
        visited = set()
        
        while queue:
            parent = queue.popleft()
            if parent in visited:
                continue
            visited.add(parent)
            
            request = NodeListRequest(parentId=parent)
            children, count = await client.list_nodes(request)
            api_calls_made += 1
            total_nodes_fetched += count
            
            for child in children:
                child_dict = child.model_dump()
                parent_id_val = child_dict.get("parent_id") or child_dict.get("parentId")
                if not parent_id_val:
                    child_dict["parent_id"] = parent
                flat_nodes.append(child_dict)
                queue.append(child.id)
        
        root_node_data = await client.get_node(node_id)
        api_calls_made += 1
        flat_nodes.insert(0, root_node_data.model_dump())
        total_nodes_fetched += 1
    else:
        raw_data = await export_nodes_impl(client, node_id)
        flat_nodes = raw_data.get("nodes", [])
        total_nodes_fetched = raw_data.get("_total_fetched_from_api", len(flat_nodes))
        api_calls_made = 1
    
    # Safety check
    subtree_node_count = len(flat_nodes)
    if max_nodes is not None and subtree_node_count > max_nodes:
        raise NetworkError(
            f"SCRY size ({subtree_node_count}) exceeds limit ({max_nodes}).\n\n"
            f"Options:\n"
            f"1. Increase max_nodes\n"
            f"2. Use max_depth or child_count_limit\n"
            f"3. Use nexus_scry + nexus_ignite_shards"
        )
    
    if not flat_nodes:
        return {
            "success": True,
            # P3 argument echo: REQUESTED string; RESOLVED rides path_record.
            "file_path": output_file,
            "node_count": 0,
            "depth": 0,
            "path_record": path_resolution.as_dict(),
        }
    
    # Build hierarchy (delegate to client method through type cast)
    if not isinstance(client, WorkFlowyClientEtch):
        raise NetworkError("Client must be WorkFlowyClientEtch for hierarchy building")
    
    hierarchical_tree = client._build_hierarchy(flat_nodes, include_metadata)

    # Extract root info
    root_node_info = None
    root_immediate_child_count = None
    if hierarchical_tree and len(hierarchical_tree) == 1:
        root_node = hierarchical_tree[0]
        
        # Build path to root
        path_uuids = [root_node.get('id')]
        path_names = [root_node.get('name')]
        
        current_parent_id = root_node.get('parent_id') or root_node.get('parentId')
        while current_parent_id:
            try:
                parent_node_data = await client.get_node(current_parent_id) or await client.get_node(current_parent_id)
                path_uuids.insert(0, parent_node_data.id)
                path_names.insert(0, parent_node_data.nm or 'Untitled')
                current_parent_id = parent_node_data.parentId
            except Exception:
                break
        
        root_node_info = {
            'id': root_node.get('id'),
            'name': root_node.get('name'),
            'note': root_node.get('note'),
            'parent_id': root_node.get('parent_id') or root_node.get('parentId'),
            'full_path_uuids': path_uuids,
            'full_path_names': path_names
        }
        root_immediate_child_count = len(root_node.get('children') or [])
        hierarchical_tree = root_node.get('children', [])
    else:
        raise NetworkError("SCRY produced multiple roots - invalid structure")
    
    # Determine root truncation
    root_has_hidden_children = False
    root_children_status = "complete"
    
    if (
        child_count_limit is not None
        and root_immediate_child_count is not None
        and root_immediate_child_count > child_count_limit
    ):
        root_has_hidden_children = True
        root_children_status = "truncated_by_count"
        hierarchical_tree = []
    
    # Annotate counts and truncate
    if hierarchical_tree:
        client._annotate_child_counts_and_truncate(
            hierarchical_tree,
            max_depth=max_depth,
            child_count_limit=child_count_limit,
            current_depth=1,
        )
    
    tree_depth = client._calculate_max_depth(hierarchical_tree)
    
    # Build ledger
    original_ids_seen = sorted({n.get('id') for n in flat_nodes if n.get('id')})
    
    # Compute preview (companions and derived identities use the RESOLVED
    # string — spec [ap:1T7JZ0] §13 row 7).
    prefix = client._infer_preview_prefix_from_path(resolved_file)
    preview_tree = None
    if prefix:
        preview_tree = client._annotate_preview_ids_and_build_tree(hierarchical_tree, prefix)
    
    # Wrap with metadata
    export_package = {
        "export_timestamp": hierarchical_tree[0].get('modifiedAt') if hierarchical_tree else None,
        "export_root_children_status": root_children_status,
        "__preview_tree__": preview_tree,
        "export_root_id": node_id,
        "export_root_name": root_node_info.get('name') if root_node_info else 'Unknown',
        # Root identity/content live beside the children-only terrain. Do not
        # insert a synthetic root into nodes: that would renumber every preview
        # ID and change the established downstream subtree contract.
        "export_root_note": root_node_info.get('note') if root_node_info else None,
        "nodes": hierarchical_tree,
        "original_ids_seen": original_ids_seen
    }

    markdown_file = resolved_file.replace('.json', '.md')
    markdown_content = client._generate_markdown(hierarchical_tree, root_node_info=root_node_info)

    # S04 Remediation Chain 1 (work-order item 4): under a CAPTURED
    # hard-dirty boundary status, the scry output ARTIFACTS carry the one
    # standardized advisory through the ONE authority (coherence.read_advisory
    # — never a copied literal, never a second applicability decision): the
    # terrain JSON gains structured ``coherence_advisory`` metadata (valid
    # JSON), and the companion Markdown is prefixed with the canonical
    # warning line + ONE blank separator.  Clean status adds nothing
    # (byte-identical artifacts).  The ``.original`` twins mirror the
    # primary artifacts exactly as written.
    from ..coherence.read_advisory import (
        apply_coherence_advisory_to_text,
        coherence_advisory_payload,
        current_boundary_status,
    )

    _scry_boundary_status = current_boundary_status()
    if _scry_boundary_status is not None and _scry_boundary_status.hard_dirty:
        export_package.update(coherence_advisory_payload(_scry_boundary_status))
        markdown_content = apply_coherence_advisory_to_text(
            markdown_content, status=_scry_boundary_status
        )

    # Write files — every destination on the RESOLVED string (the fan-out
    # keeps its per-site json.dump/text shapes; only the path authority
    # changed — spec [ap:1T7JZ0] §6.0 Q1).
    with open(resolved_file, 'w', encoding='utf-8') as f:
        json.dump(export_package, f, indent=2, ensure_ascii=False)

    json_backup = resolved_file.replace('.json', '.original.json')
    with open(json_backup, 'w', encoding='utf-8') as f:
        json.dump(export_package, f, indent=2, ensure_ascii=False)

    with open(markdown_file, 'w', encoding='utf-8') as f:
        f.write(markdown_content)
    
    markdown_backup = resolved_file.replace('.json', '.original.md')
    with open(markdown_backup, 'w', encoding='utf-8') as f:
        f.write(markdown_content)

    # Create Keystone
    keystone_path = None
    try:
        import shutil
        import uuid

        backup_dir = str(get_runtime_subdir("nexus_backups"))
        os.makedirs(backup_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        node_name_slug = "".join(c for c in root_node_info.get('name', 'Unknown') if c.isalnum() or c in " _-").rstrip()[:50]
        short_uuid = str(uuid.uuid4())[:6]

        keystone_filename = f"{timestamp}-{node_name_slug}-{short_uuid}.json"
        keystone_path = os.path.join(backup_dir, keystone_filename)

        shutil.copy2(resolved_file, keystone_path)
    except Exception as e:
        print(f"Keystone backup failed: {e}")

    return {
        "success": True,
        # P3: file_path is the argument echo (REQUESTED, byte-identical);
        # markdown_file is derived (RESOLVED). keystone_backup_path is a
        # runtime-directory path (neither an argument echo nor derived from
        # the argument). path_record carries the resolution structure.
        "file_path": output_file,
        "markdown_file": markdown_file,
        "keystone_backup_path": keystone_path,
        "node_count": len(flat_nodes),
        "depth": tree_depth,
        "total_nodes_fetched": total_nodes_fetched,
        "api_calls_made": api_calls_made,
        "efficient_traversal": use_efficient_traversal,
        "path_record": path_resolution.as_dict(),
    }
