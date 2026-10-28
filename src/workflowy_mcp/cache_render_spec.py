"""RenderProjectionSpec — the one normalized cache-render request object.

WHY THIS EXISTS
---------------
Every cache-projected render (the (slice_group, root_uuid) truth table +
Lightning Rod) normalizes to ONE explicit request object with clear defaults.
Only override fields are supplied at the external/API boundaries; ``render_
scope`` is effectively the serialized form of this object. Consolidating the
request into one object (rather than a long list of independent scalar flags --
``match_scope``, ``descend_policy``, ``root_uuid``, ``lightning``,
sort/since/headings -- threaded through every caller: ``glimpse`` →
``_glimpse_push_slice_to_modal`` → ``apply_slice_in_dock`` →
``applySliceGroupIntoDock`` → ``requestSliceModalRender`` → the modal handler,
plus their per-row / refresh twins) means adding a new render knob touches the
object and the sites that read it, not every caller in the chain.

THE TWO ORTHOGONAL AXES this object separates cleanly (never conflate them):
  * SELECTION predicate  — ``match_scope`` (WHERE to look for a tag/text match).
  * TRAVERSAL policy     — ``descend_policy`` + ``exclude_completed`` (how far /
                           whether to descend). These are Phase-1 traversal
                           concerns and must NEVER be folded into a Lightning
                           Rod ``selection`` (which is about what matches) nor
                           pushed into Phase 2/3/4 (presentation policy).

The dataclass is intentionally ``frozen`` so a spec is an immutable value that
can be passed around, echoed, and compared without any caller mutating it out
from under another.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal

# The server-side completed-node exclusion default lives in f8_slice_policy as a
# module constant (a fixed policy for now, not an agent-facing flag). We mirror
# it as the spec's ``exclude_completed`` default so the normalized object agrees
# with the current pipeline behavior by construction.
from .f8_slice_policy import SERVER_SIDE_GLIMPSE_EXCLUDE_COMPLETED
from .glimpse_since_policy import (
    SinceSpec,
    since_from_wire,
    since_to_wire,
)

# ---------------------------------------------------------------------------
# Canonical vocabularies (kept as plain tuples so both the dataclass validation
# and the callers can share one source of truth).
# ---------------------------------------------------------------------------
SPEC_KINDS: tuple[str, ...] = ("raw_root", "slice", "slice_override", "lightning_rod")
MATCH_SCOPES: tuple[str, ...] = ("name_and_note", "name")
DESCEND_POLICIES: tuple[str, ...] = ("all", "stop_at_child_anchors")
SORT_BYS: tuple[str, ...] = ("none", "created", "modified")
SORT_DIRECTIONS: tuple[str, ...] = ("asc", "desc")
CONTENT_FORMATS: tuple[str, ...] = ("markdown", "raw_text")
TREE_MODES: tuple[str, ...] = ("render_tree", "root_only")

Kind = Literal["raw_root", "slice", "slice_override", "lightning_rod"]
MatchScope = Literal["name_and_note", "name"]
DescendPolicy = Literal["all", "stop_at_child_anchors"]

# Recognized render-focus modes. ``row`` is a single occurrence-exact
# tree-view row; ``rows`` (Phase 1 multi-select, ap:MULTISEL) is a SET of
# occurrence-exact rows whose MINIMAL COVERING SUBTREE is rendered. The field
# is a dict so a new focus kind adds a mode without changing the wire contract.
# ``row`` is deliberately kept as its own mode (NOT folded into a 1-element
# ``rows``) so single-select stays byte-for-byte the pre-multi-select path --
# the regression anchor.
FOCUS_MODES: tuple[str, ...] = ("row", "rows")


# @beacon[
#   id=cache-render-spec@focus-resolution-error,
#   slice_labels=nexus--md-preview,
#   kind=ast,
#   comment=Phase 0.5 (ap:EXECLDG §5-Correction#3 / §9) EXACT-OR-ERROR focus contract exception. Raised by materialize_from_document_forest when a CANONICAL row focus ({mode:'row', uuid, ancestor_path, occurrence} that survived _canonicalize_focus) FAILS to resolve occurrence-exactly against the materialized document_forest. There is deliberately NO lenient fallback: not the whole document, not the first occurrence, not a uuid-set match -- a silent whole-document degrade would show the agent/user content they did NOT intend, INVISIBLY, which is exactly what this contract forbids. Only a genuinely ABSENT focus (None -- _canonicalize_focus already maps malformed/empty to None) renders the whole document. Callers (Steps 6/7) catch this to emit the actionable slice_row_stale / refresh-cache message rather than crash. Distinct from f8_slice_policy.SliceProjectionInvariantError (a Phase-4 broken-invariant guard); this is a stale-focus signal, not a corruption signal.,
# ]
class FocusResolutionError(RuntimeError):
    """A canonical row focus failed to resolve exactly (exact-or-error).

    Raised by :func:`materialize_from_document_forest` when a real
    ``{mode:'row', uuid, ancestor_path, occurrence}`` focus cannot be resolved
    occurrence-exactly against the ``document_forest``. Per the Phase-0.5
    exact-or-error contract (§5-Correction#3 / §9) there is NO lenient
    whole-document / first-occurrence / uuid-set fallback -- a stale focus
    fails LOUD so a caller surfaces the refresh-cache / re-apply message
    instead of silently rendering the wrong content. Only an ABSENT focus
    (``None``) renders the whole document.
    """

# Recognized body-suppression CAUSES for RenderPolicy (Phase 0.5). The cause is
# LOAD-BEARING, not merely diagnostic: it dictates the KEYING DISCIPLINE and
# WHICH renderer consumes the entry (v3.1 #2 / v3.3 #1).
#   * ``breadcrumb`` — a focus breadcrumb ancestor renders heading-only. Keyed
#     by the RENDER-FOREST occurrence (the list-rooted ``_index_path`` tuple),
#     NOT bare uuid, because under a timestamp sort the same uuid re-emits and
#     only SOME occurrences are breadcrumb ancestors of the focus. Consumed by
#     BOTH the Markdown composer AND (post-Step-7) the full/UUID serializer.
#   * ``deprecated`` — a surviving ``--deprecated`` node renders header-only
#     (ap:DEPBOD). A property of the NODE, so keyed by UUID and correctly
#     suppressed at every occurrence. Consumed by the Markdown composer ONLY in
#     Phase 0.5 (the full/UUID serializer deliberately does NOT honor it —
#     v3.1 #2 / v3.2(a) — a surviving --deprecated node still shows its [note]
#     in full mode unless that is separately signed off).
#   * ``chronological_ancestor`` — U6 (ap:E03BN3, Dan's ruling: *any
#     chronological sort of any form never displays ancestor notes*): on a
#     CHRONOLOGICAL render (``glimpse_since_policy.render_is_chronological`` —
#     a resolved created/modified sort in either direction, or ANY ``since``:
#     a window/curated on the SinceSpec's own axis, or an item-count tail
#     since CF-13 — 'newest N'/'last N' and the positional 'bottom N' alike;
#     the excerpt order stays document order unless the caller sorts), EVERY
#     non-leaf of the final render forest
#     (the root included when non-leaf) renders heading-only. OCCURRENCE /
#     index-path keyed exactly like ``breadcrumb`` (a sorted render re-emits
#     the same ancestor uuid at multiple index paths — uuid keying would be a
#     real bug), computed ONLY inside ``materialize_from_document_forest``
#     from the producer-supplied ``chronological`` boolean. Consumed by BOTH
#     renderers (occurrence-local causes follow the breadcrumb precedent; the
#     full/UUID serializer leaks the same 25 KB inline ``[note]`` otherwise).
#     Member set, ordering and leaf identity are untouched — only what gets
#     PRINTED. The ``RenderProjectionSpec.ancestor_notes`` teaching flag
#     (default false) is the sole opt-out.
#   * ``lens_scaffold_ancestor`` — [ap:I506WY] (unit 5g): on a LENS render
#     (slice_group.render / sift / merge.render, or any render_lens
#     selection) the connective SCAFFOLDING above the lens's input roots (the
#     LCA down to each input root) renders heading-only EVEN IN DOCUMENT
#     order — a distant-tree union must not drag in every ancestor's full
#     note. Input roots and their subtrees are unaffected. OCCURRENCE /
#     index-path keyed exactly like ``breadcrumb`` and
#     ``chronological_ancestor`` (a sorted render re-emits the same uuid;
#     uuid keying would be the same bug class), computed ONLY inside
#     ``materialize_from_document_forest`` from the producer-supplied
#     ``lens_scaffold_ids`` id set (computed once by the shared lens
#     projection ``lens_engine.lens_scaffold_ancestor_ids`` over the final
#     tree + the per-atom resolved roots). Consumed by BOTH renderers through
#     the same render_key branch. The ``ancestor_notes`` opt-out (default
#     false) restores the ancestor notes on sift / merge.render / render.
BODY_SUPPRESSION_CAUSES: tuple[str, ...] = (
    "breadcrumb",
    "deprecated",
    "chronological_ancestor",
    "lens_scaffold_ancestor",
)

# The render-forest occurrence key type. The occurrence-exact ``_index_path``
# (:meth:`WorkFlowyClientNexus._iter_markdown_focus_rows`) is a list of child
# indices; as a policy key it is a TUPLE of ints (hashable + comparable). This
# is the ONE coordinate system the focused composer prunes by, so keying the
# breadcrumb suppression + the per-file rebase baseline in it guarantees the
# policy lands on exactly the occurrence the renderer is emitting.
RenderKey = tuple[int, ...]


# @beacon[
#   id=cache-render-spec@canonicalize-focus,
#   slice_labels=nexus--md-preview,glimpse-render,
#   kind=ast,
#   comment=THE one normalizer for the render-FOCUS primitive. Coerces a loose focus value into the canonical {mode:'row', uuid, ancestor_path, occurrence} dict, or None (= the full unfocused document). Lenient with a SAFE fallback to None: a non-dict / unknown-mode / empty-uuid focus degrades to 'no focus' rather than mis-rendering. Single entry point used by RenderProjectionSpec.normalize_from_glimpse_args (and, through it, the blob/modal-frame deserializers) AND imported at module scope by server.py _compose_render_scope so the render_scope focus echo is canonicalized identically wherever a focus enters -- one shape, one source of truth, no drift. Pure; no I/O. FOCUS_MODES is the recognized-modes tuple ('row' only today; the dict shape lets a future kind be added without a wire change).,
# ]
def _canonicalize_focus(focus: Any) -> dict[str, Any] | None:
    """Coerce a loose ``focus`` value into the canonical row-focus dict (or None).

    The ONE normalizer for the render-focus primitive, used by every entry
    point (``normalize_from_glimpse_args`` and, through it, the blob/modal-frame
    deserializers) so a focus is shaped identically no matter where it enters.
    Lenient with a SAFE fallback to ``None`` (= the full, unfocused document):
    an unusable focus never mis-renders, it simply degrades to "no focus".

    Canonical shape::

        {"mode": "row", "uuid": str, "ancestor_path": [str, ...], "occurrence": int}

    A focus is DROPPED (-> ``None``) when it is not a dict, its ``mode`` is
    unrecognized, or (for ``mode='row'``) it carries no non-empty ``uuid`` --
    a row focus with no target node is meaningless. ``ancestor_path`` is
    coerced to a list of non-empty strings (root-first, self excluded, matching
    ``_iter_markdown_focus_rows``); ``occurrence`` to a non-negative int
    (default 0, matching the legacy unique-uuid callers). Pure; no I/O.
    """
    if not isinstance(focus, dict):
        return None
    mode = str(focus.get("mode") or "row").strip().lower()
    if mode not in FOCUS_MODES:
        return None

    def _canon_row(row: Any) -> dict[str, Any] | None:
        """Canonicalize ONE {uuid, ancestor_path, occurrence} row, or None."""
        if not isinstance(row, dict):
            return None
        u = str(row.get("uuid") or "").strip()
        if not u:
            return None
        ap = [
            str(p).strip()
            for p in (row.get("ancestor_path") or [])
            if str(p).strip()
        ]
        try:
            o = int(row.get("occurrence") or 0)
        except (TypeError, ValueError):
            o = 0
        if o < 0:
            o = 0
        return {"uuid": u, "ancestor_path": ap, "occurrence": o}

    if mode == "rows":
        # Phase 1 multi-select (ap:MULTISEL): a SET of occurrence-exact rows.
        # Each element is canonicalized by the SAME rules as a singleton row;
        # unusable elements are dropped. De-dup EXACT (uuid, ancestor_path,
        # occurrence) triples (a double-toggle of the same row must not weight
        # it twice) while preserving first-seen order. A rows-focus with ZERO
        # usable rows degrades to None (= the whole unfocused document),
        # matching the singleton's empty-uuid -> None safe fallback.
        canon_rows: list[dict[str, Any]] = []
        seen: set[tuple[str, tuple[str, ...], int]] = set()
        for row in (focus.get("rows") or []):
            cr = _canon_row(row)
            if cr is None:
                continue
            key = (cr["uuid"], tuple(cr["ancestor_path"]), cr["occurrence"])
            if key in seen:
                continue
            seen.add(key)
            canon_rows.append(cr)
        if not canon_rows:
            return None
        return {"mode": "rows", "rows": canon_rows}

    # mode == 'row' (the single-select kind; kept byte-for-byte identical to
    # the pre-multi-select path -- the regression anchor).
    uuid = str(focus.get("uuid") or "").strip()
    if not uuid:
        return None
    ancestor_path = [
        str(p).strip()
        for p in (focus.get("ancestor_path") or [])
        if str(p).strip()
    ]
    try:
        occ = int(focus.get("occurrence") or 0)
    except (TypeError, ValueError):
        occ = 0
    if occ < 0:
        occ = 0
    return {
        "mode": "row",
        "uuid": uuid,
        "ancestor_path": ancestor_path,
        "occurrence": occ,
    }


# @beacon[
#   id=cache-render-spec@render-policy,
#   slice_labels=nexus--md-preview,glimpse-render,
#   kind=ast,
#   comment=RenderPolicy — the immutable, RENDER-FOREST-KEYED carrier of the occurrence-local render directives a focused/deprecated/chronological render needs, DECOUPLED from the tree: (1) the per-file/heading H1 rebase baseline, keyed by render-forest _index_path (covers BOTH the file-node root and the bare-MD-heading root); and (2) body suppression, carried as FOUR CAUSE-LABELED sets in DISTINCT keying disciplines — breadcrumb suppression is OCCURRENCE/index-path keyed (NOT a uuid-set, because a timestamp sort re-emits the same uuid and only SOME occurrences are focus breadcrumbs), deprecated-body suppression is uuid keyed (a per-node property), chronological-ancestor suppression (U6, ap:E03BN3) is OCCURRENCE/index-path keyed like breadcrumb (every non-leaf of the final render forest on a chronological render — computed ONLY inside the materializer from the producer-supplied chronological boolean), and lens-scaffold-ancestor suppression ([ap:I506WY], unit 5g) is OCCURRENCE/index-path keyed too (on a LENS render the connective scaffolding above the lens's input roots renders heading-only even in document order; computed ONLY inside the materializer from the producer-supplied lens_scaffold_ids the shared lens projection derives). The cause label is load-bearing: emit_body_false_for(render_key, uuid) lets BOTH renderers consume the THREE occurrence-keyed causes (breadcrumb ∪ chronological-ancestor ∪ lens-scaffold-ancestor) via the render_key branch, while deprecated-body suppression stays Markdown-only (include_deprecated). It is the policy the materializer fills and the composer/serializer consume, in place of loose side-channel params (focus_index_path / body_suppressed_ids / inline _min_md_depth_in_subtree). Frozen value object, no I/O; a sibling of RenderProjectionSpec (the request) — this is the derived RESULT-side policy. Also carries the UUID-keyed source-graph facts: direct_child_counts, and ([ap:0M6VP8] part a) uuid_prefix_collisions (the tiny 8-hex->colliding-ids index the headings-only serializer lengthens ambiguous short ids through, via the ONE unique_short_uuid_prefix authority) + full_uuid_headings (the full_uuids opt-in's result-side copy) + ([ap:UH0UOU]) source_metrics (the render-side four-field headings-vocabulary index — dock.peek's ▸/desc/self/tree from build_render_source_metrics over the same pinned cache; the composer's ONE heading-metadata emitter consumes it and suppresses the whole suffix on managed source lines).,
# ]
@dataclass(frozen=True)
class RenderPolicy:
    """Render-forest-keyed, occurrence-local render directives (Phase 0.5).

    A small immutable value object carrying the render POLICY facts the focused
    / deprecated render needs, decoupled from the node tree (§4: focus is
    render POLICY, never tree mutation — no renderer/serializer ever receives a
    tree whose notes were blanked for display). It is the seed of
    ``MaterializedRenderDocument.policy_by_occurrence`` (Step 5) and is
    introduced UNWIRED in Step 3: nothing populates or reads it yet.

    SOURCE CHILD COUNTS
      * ``direct_child_counts`` — total direct children per known cache UUID,
        including completed/suppressed/out-of-slice children. Originators
        compute it once from their pinned source snapshot, never from the
        projected/focused tree. Both headings-only serializers consume it;
        absent metadata prints an explicit unknown marker rather than zero.

    The fields carry rebase, body-suppression, file-boundary and source-count
    presentation facts; none changes request identity or Stage-1 membership.

    REBASE BASELINE
      * ``rebase_by_render_key`` — ``{RenderKey: base_depth}``. The per-file /
        per-heading H1 rebase baseline, keyed by the RENDER-FOREST
        ``_index_path`` of the file/heading ROOT whose subtree it anchors.
        Covers BOTH the Cartographer file-node root AND the bare-MD-heading
        root (v3.3 #2), so hazard-b (a focus prune removing off-path siblings
        shifting the shallowest-visible depth) is impossible by construction:
        the baseline is computed from the UNPRUNED document_forest and the
        composer consumes it instead of recomputing ``_min_md_depth_in_subtree``
        inline over the (possibly pruned) render_forest.

    BODY SUPPRESSION (three causes, DISTINCT keying — v3.3 #1 / U6)
      * ``breadcrumb_emit_body_false`` — ``set[RenderKey]``. Focus-breadcrumb
        ancestors that render heading-only. OCCURRENCE/index-path keyed (NOT a
        uuid-set): under a timestamp sort the same uuid re-emits and only SOME
        occurrences are breadcrumb ancestors of the focus, so a uuid-set would
        wrongly suppress the body at EVERY occurrence (re-introducing
        symptom #5 in full mode). Consumed by BOTH renderers.
      * ``deprecated_emit_body_false`` — ``set[str]`` of UUIDs. Surviving
        ``--deprecated`` nodes that render header-only (ap:DEPBOD). Deprecation
        is a per-NODE property, so uuid keying is correct (suppressed at every
        occurrence). Consumed by the Markdown composer ONLY in Phase 0.5; the
        full/UUID serializer deliberately ignores this cause (v3.1 #2 /
        v3.2(a)).
      * ``chronological_ancestor_emit_body_false`` — ``set[RenderKey]`` (U6,
        ap:E03BN3; Dan's ruling: *any chronological sort of any form never
        displays ancestor notes*). On a CHRONOLOGICAL render, every NON-LEAF
        occurrence of the final render forest (the root included when non-leaf)
        renders heading-only; leaf notes print. OCCURRENCE/index-path keyed
        exactly like ``breadcrumb`` — a sorted render re-emits the same
        ancestor uuid at multiple index paths, so a uuid-set would suppress
        the body at occurrences that are leaves there (a real bug, not a
        variant). Computed ONLY inside ``materialize_from_document_forest``
        (strict prefixes of ``collect_leaf_branches(render_forest)``) when the
        producer passes ``chronological=True``; empty on every
        non-chronological render, so behavior is byte-identical to before.
        Consumed by BOTH renderers (the full/UUID serializer leaks the same
        ancestor notes inline otherwise). Membership, ordering and leaf
        identity are never affected — only what gets PRINTED.
      * ``lens_scaffold_ancestor_emit_body_false`` — ``set[RenderKey]``
        ([ap:I506WY], unit 5g). On a LENS render, the connective scaffolding
        ABOVE the lens's input roots (the LCA down to each input root) renders
        heading-only EVEN IN DOCUMENT order; input roots and their subtrees
        are unaffected. OCCURRENCE/index-path keyed exactly like
        ``chronological_ancestor``; computed ONLY inside
        ``materialize_from_document_forest`` from the producer-supplied
        ``lens_scaffold_ids`` id set (the shared lens projection computes it
        once). Consumed by BOTH renderers. Empty on every non-lens render and
        on a lens render whose caller passed the ``ancestor_notes`` opt-out.

    The cause labels survive INTO this object (not just debug) precisely
    because they determine the keying discipline AND which renderer consumes
    each entry — see :meth:`emit_body_false_for`.

    FILE-BOUNDARY PRESENTATION (ap:MSFBP, Phase 1 multi-select)
      * ``file_boundary_mode`` — ``'standalone_blocks'`` (DEFAULT) or
        ``'connected_subtree'``. A DOCUMENT-WIDE (not occurrence-local)
        directive: the default reproduces today's anthology-style file
        rendering (file + contents reset to H1, ``---`` separators, post-file
        breadcrumb delimiter) byte-for-byte; ``'connected_subtree'`` — used
        ONLY by ``focus={mode:'rows'}`` covering renders — renders each file
        node at its natural structural level with its contents nested beneath
        it, no ``---`` breaks, and no post-file breadcrumb delimiter, so a
        multi-select spanning files reads as one connected subtree with its
        shared spine printed once. It is a RESULT-side presentation fact, NOT
        a request-identity field (never on RenderProjectionSpec).
    """

    rebase_by_render_key: dict[RenderKey, int] = field(default_factory=dict)
    breadcrumb_emit_body_false: frozenset[RenderKey] = field(
        default_factory=frozenset
    )
    # U6 (ap:E03BN3): the THIRD body-suppression cause — chronological-ancestor.
    # OCCURRENCE/index-path keyed (never uuid keyed — a sorted render re-emits
    # the same ancestor uuid at multiple index paths). Filled ONLY by
    # materialize_from_document_forest when the producer signals
    # chronological=True; consulted by BOTH renderers through
    # emit_body_false_for's render_key branch. Empty (the default) on every
    # non-chronological render => byte-identical prior behavior.
    chronological_ancestor_emit_body_false: frozenset[RenderKey] = field(
        default_factory=frozenset
    )
    # [ap:I506WY] (unit 5g): the FOURTH body-suppression cause — lens-scaffold
    # ancestor. OCCURRENCE/index-path keyed (never uuid keyed — a sorted
    # render re-emits the same ancestor uuid at multiple index paths). Filled
    # ONLY by materialize_from_document_forest when the producer passes the
    # lens's ``lens_scaffold_ids`` (computed once by
    # lens_engine.lens_scaffold_ancestor_ids over the final tree + the
    # per-atom resolved roots); consulted by BOTH renderers through
    # emit_body_false_for's render_key branch. Empty (the default — every
    # non-lens render, and every lens render whose caller passed the
    # ancestor_notes opt-out) => byte-identical prior behavior.
    lens_scaffold_ancestor_emit_body_false: frozenset[RenderKey] = field(
        default_factory=frozenset
    )
    deprecated_emit_body_false: frozenset[str] = field(default_factory=frozenset)
    # FILE-BOUNDARY PRESENTATION POLICY (ap:MSFBP, Phase 1 multi-select).
    # A DOCUMENT-WIDE (not occurrence-local) presentation directive selecting
    # which of the two file-boundary regimes the composer's is_file_node branch
    # uses:
    #   * 'standalone_blocks' (DEFAULT) -- today's behavior for EVERY ordinary
    #     GLIMPSE / slice / single-row-focus / DOM path: the file node + its
    #     contents reset to H1, file blocks are separated by '---' rules, and a
    #     post-file BREADCRUMB DELIMITER re-anchors the reader into the global
    #     tree after a file block closes. This is the correct anthology-style
    #     read for ordinary cross-file GLIMPSE and MUST stay byte-for-byte
    #     identical (golden-locked).
    #   * 'connected_subtree' (NEW) -- ONLY focus={mode:'rows'} minimal covering
    #     renders (the multi-select dock gesture): shared ancestors render
    #     ONCE; each file node renders at its NATURAL STRUCTURAL level (not H1);
    #     file contents (md headings AND source AST descendants) rebase UNDER
    #     the file node; the abs-path leader is still emitted; NO post-file
    #     breadcrumb delimiter and NO root-level '---' breaks (the hierarchy IS
    #     the separator). Reads as one properly-nested connected document.
    # This is a RESULT-side presentation fact (a RenderPolicy directive), NOT a
    # request-identity/wire field -- it never belongs on RenderProjectionSpec.
    # The materializer sets it to 'connected_subtree' exactly when the
    # canonical focus mode == 'rows'; every other path keeps the default.
    file_boundary_mode: str = "standalone_blocks"
    # Source-graph facts, UUID-keyed (not occurrence-local). Computed once
    # from the originator's pinned cache map and shared across row renders.
    # None / missing UUID means UNKNOWN, never zero inferred from projection.
    # Presentation only: not a RenderProjectionSpec flag or wire field.
    direct_child_counts: dict[str, int] | None = None
    # SHORT HEADING IDS ([ap:0M6VP8] part a + CF-22/O3): the 8-hex ->
    # colliding-ids index over the SAME pinned cache map direct_child_counts
    # was built from (only ambiguous prefixes are present). The headings-only
    # serializer lengthens a colliding [id] just enough to be unique through
    # the ONE authority unique_short_uuid_prefix; absent/None (an ad-hoc
    # DOM render with no cache operand) keeps the plain 8-hex form. Built once
    # per render next to the child counts; presentation only.
    uuid_prefix_collisions: dict[str, list[str]] | None = None
    # The full_uuids opt-in ([ap:0M6VP8]): when True the headings-only [id]
    # token prints the FULL UUID instead of the short form. RESULT-side copy
    # of the spec's full_uuids VIEW knob (like ancestor_notes: the producer
    # folds the request flag into the policy the renderers consume).
    full_uuid_headings: bool = False
    # REPOSITORY VIEW (U1, MasterSpec §5.7): the result-side copy of the
    # repository_view request flag, set ONLY by materialize_from_document_
    # forest (repository_view=True) on the policy it builds. The composer's
    # three gates (G1 heading text verbatim -- no DOM strip, no [id], no
    # metrics; G2 every node classified structural -- no file branch, no
    # MD-heading branch, no sentinel, no breadcrumb delimiter; G3 the body
    # is the note verbatim -- no metadata strip, no hazard fence, no
    # embedded-Markdown branch) read it via getattr(..., False), so every
    # policy built without the flag keeps today's bytes exactly.
    repository_view: bool = False
    # END-OF-LINE METADATA ([ap:UH0UOU]): the UUID-keyed source-metrics index
    # (build_render_source_metrics over the SAME pinned cache map the other
    # source-graph facts came from) behind the four-field headings vocabulary
    # dock.peek prints (▸children · desc:N · self: · tree:). Consumed ONLY by
    # the composer's ONE heading-metadata emitter; None (an ad-hoc render with
    # no cache operand) keeps the child-count-only suffix, and a managed
    # source line (managed_metadata.is_managed_source_line) suppresses the
    # whole suffix. Presentation only, like every source-graph fact here.
    source_metrics: "dict[str, dict[str, dict[str, int]]] | None" = None

    def rebase_base_for(self, render_key: RenderKey) -> int | None:
        """The H1 rebase baseline for a file/heading root at ``render_key``.

        Returns the stored base depth, or ``None`` when this render key has no
        rebase entry (the composer then falls back to its own default, exactly
        as today). ``render_key`` is coerced to a tuple so a caller passing a
        list still matches.
        """
        return self.rebase_by_render_key.get(tuple(render_key))

    def emit_body_false_for(
        self,
        *,
        render_key: RenderKey | None = None,
        uuid: str | None = None,
        include_deprecated: bool = True,
    ) -> bool:
        """Should this occurrence's NOTE BODY be WITHHELD?

        The ONE consult point both renderers call, so the cause-keying
        discipline lives in exactly one place:

          * ``render_key`` (a RenderKey / index_path) tests ALL THREE
            OCCURRENCE-keyed causes — the ``breadcrumb`` cause (a focus
            breadcrumb ancestor), since U6 the ``chronological_ancestor``
            cause (a non-leaf of the final forest on a chronological render),
            and, since [ap:I506WY], the ``lens_scaffold_ancestor`` cause (a
            scaffold node above a lens's input roots). Both renderers pass
            this.
          * ``uuid`` tests the uuid-keyed ``deprecated`` cause — a surviving
            ``--deprecated`` node. Only the Markdown composer consults it; the
            full/UUID serializer passes ``include_deprecated=False`` so DEPBOD
            stays Markdown-only in Phase 0.5 (v3.1 #2 / v3.2(a)).

        Returns True iff EITHER applicable cause matches. Suppressing the body
        is composable with every other body gate (headings_only, etc.) exactly
        like the current per-node ``_body_suppressed`` boolean — the heading
        still renders; only the body is withheld.
        """
        if render_key is not None:
            _render_key = tuple(render_key)
            if _render_key in self.breadcrumb_emit_body_false:
                return True
            # U6 (ap:E03BN3): the chronological-ancestor cause is
            # occurrence-keyed exactly like breadcrumb, so it rides the SAME
            # render_key branch at the SAME gates — no second suppression
            # path, no gate changes in either renderer. Empty set (every
            # non-chronological render) => this test never fires.
            if _render_key in self.chronological_ancestor_emit_body_false:
                return True
            # [ap:I506WY] (unit 5g): the lens-scaffold-ancestor cause rides
            # the SAME occurrence-keyed branch at the SAME gates — the
            # scaffolding above a lens's input roots renders heading-only,
            # both renderers, no second suppression path. Empty set (every
            # non-lens render, and a lens render whose caller passed the
            # ancestor_notes opt-out) => this test never fires.
            if _render_key in self.lens_scaffold_ancestor_emit_body_false:
                return True
        if (
            include_deprecated
            and uuid is not None
            and str(uuid) in self.deprecated_emit_body_false
        ):
            return True
        return False


# @beacon[
#   id=cache-render-spec@materialized-render-document,
#   slice_labels=nexus--md-preview,glimpse-render,
#   kind=ast,
#   comment=Phase 0.5 Step 5 (ap:EXECLDG) MaterializedRenderDocument -- the immutable, SERVER-INTERNAL result object that IS the materialized answer to applying a spec (or a DOM capture) to a concrete source forest. It NEVER crosses the wire (unlike RenderProjectionSpec, the request handle): the client keeps sending a spec and keeps receiving the same serialized outputs (preview_markdown / preview_tree / rows / render_scope) -- this object is built server-side, rendered FROM, and discarded. It is the cohesive replacement for the old ever-growing bag of loose composer side-channel params (focus_path / focus_leaf / focus_index_path / body_suppressed_ids / inline _min_md_depth_in_subtree). Fields: document_forest (the complete projected+sorted+since forest -- AUTHORITY for rows, row identity/occurrence, focus resolution, and the rebase baseline; NEVER shrunk by focus); render_forest (exactly what the bottom renderer consumes: whole document for focus None, or the note-PRESERVING option-Y breadcrumb+subtree for a singleton focus -- notes are NEVER blanked, per v3.1 hazard-a); policy_by_occurrence (a RenderPolicy keyed in RENDER-FOREST coordinates -- the rebase baseline VALUES come from the unpruned document_forest but are re-keyed to render coords per v3.3 #1/#2, and the breadcrumb body-suppression set is occurrence/index-path keyed); document_rows (derived from document_forest so focusing never shrinks the top-half list); focus (canonical row-focus dict or None); node_count/depth (focused stats over render_forest). Frozen value object; introduced in Step 5 and WIRED through Steps 6-12b: every production render path (render_stage2_filter_sort, _render_slice_row_view, compose_markdown_modal_view, the DOM-capture dispatch) materializes through it, and the composer/serializer consume render_forest + policy_by_occurrence.,
# ]
@dataclass(frozen=True)
class MaterializedRenderDocument:
    """The server-internal materialized result of a cache render (Phase 0.5).

    NEVER crosses the wire. Where :class:`RenderProjectionSpec` is the render
    REQUEST (client↔server), this is the materialized ANSWER the server builds,
    renders from, and discards. It replaces the old scatter of loose composer
    side-channel params with ONE cohesive value that *is* the render document.

    FIELDS
      * ``document_forest`` -- the COMPLETE projected (+sorted +since) forest.
        AUTHORITY for: the top-half rows, row identity + occurrence numbering,
        focus resolution, AND the per-file/heading rebase baseline. It is
        NEVER destructively shrunk by focus, so a baseline (or a row) computed
        from it is immune to a focus prune removing an off-path sibling
        (hazard-b).
      * ``render_forest`` -- exactly the forest the bottom renderer consumes:
        the whole document (``focus`` None), or the option-Y breadcrumb +
        focused subtree (a singleton ``focus``). ALL RETAINED NOTES ARE INTACT
        -- never blanked for display (v3.1 hazard-a); body suppression is a
        render POLICY (see ``policy_by_occurrence``), not a tree mutation.
      * ``policy_by_occurrence`` -- a :class:`RenderPolicy` keyed in
        RENDER-FOREST coordinates. Its rebase baseline VALUES are computed
        from the unpruned ``document_forest`` (hazard-b immune) and re-keyed to
        render-forest ``_index_path`` (v3.3 #2), and its body-suppression sets
        follow the RenderPolicy keying discipline (breadcrumb AND, since U6,
        chronological-ancestor = occurrence / index-path keyed; deprecated =
        uuid keyed -- v3.3 #1).
      * ``document_rows`` -- the ordered top-half tree-view rows, derived from
        ``document_forest`` (NOT the focused subset), so focusing the bottom
        pane never shrinks / renumbers the top-half list.
      * ``focus`` -- the canonical row-focus dict
        (``{mode:'row', uuid, ancestor_path, occurrence}``) or ``None`` (whole
        document).
      * ``node_count`` / ``depth`` -- focused stats computed over
        ``render_forest``.

    Frozen: an immutable value passed around, rendered from, and compared
    without any consumer mutating it. Introduced in Step 5; wired into every
    production render path by Step 12b.
    """

    document_forest: list[dict[str, Any]] = field(default_factory=list)
    render_forest: list[dict[str, Any]] = field(default_factory=list)
    policy_by_occurrence: RenderPolicy = field(default_factory=RenderPolicy)
    document_rows: list[dict[str, Any]] = field(default_factory=list)
    focus: dict[str, Any] | None = None
    node_count: int = 0
    depth: int = 0


# @beacon[
#   id=cache-render-spec@render-projection-spec,
#   slice_labels=nexus--md-preview,glimpse-render,
#   kind=ast,
#   comment=THE normalized cache-render request object -- the keystone of the cache-render pipeline. ONE immutable, frozen dataclass every cache-projected render (raw root dump / slice / slice+override / Lightning Rod) normalizes to before projection+render, so the same shared render pipeline is driven identically from EITHER origin: the SERVER (glimpse / lightning_rod MCP tools, and the push twin _glimpse_push_slice_to_modal) OR the CLIENT (Apply button, Refresh, row click, Lightning Rod ribbon). The origin fills the object with what it already knows and passes it through -- the server NEVER queries the client for a value it can supply itself (a server push forwards headings_only explicitly rather than inheriting the client dock toggle). Field groups: SOURCE/SCOPE identity (kind + root_uuid + group_id/group_name + opaque lightning selection); SELECTION/TRAVERSAL knobs (match_scope = WHERE to look for a tag/text match; descend_policy = HOW FAR to walk, a Phase-1 traversal boundary; exclude_completed); VIEW knobs (sort_by [wire spelling 'none'|'created'|'modified'] / sort_direction / since / recent_by ([ap:TVIIBN] 4d(a): the recency basis of a 'newest' item_count selection; None = resolve at apply) / headings_only / ancestor_notes [U6 teaching opt-out of the chronological ancestor-note suppression, default false]); OUTPUT/RENDER contract (as_markdown / content_format / tree_mode). Two orthogonal axes kept strictly separate and NEVER conflated: match_scope (selection predicate) vs descend_policy+exclude_completed (traversal) -- and descend_policy lives at the TOP LEVEL, never inside a Lightning Rod selection (which is about WHAT matches, not how far to walk) and never in Phase 2/3/4 (presentation policy). Two classmethod normalizers -- normalize_from_glimpse_args (the glimpse/lightning_rod public-arg surface) and normalize_from_modal_frame (an incoming client Apply/push frame) -- both encode the (slice_group, root_uuid, lightning) -> kind truth table identically (lightning>override>slice>raw_root); from_render_spec_blob deserializes the opaque wire blob through that SAME normalizer. sort_by_internal() is the ONE wire('none')<->internal(None) translation; to_render_scope_fields() serializes the scope-echo knobs for the server render_scope echo; to_render_spec_blob() serializes the FULL spec (incl. the opaque lightning selection) as the nested render_spec wire blob. WIRE CONTRACT: the nested render_spec blob is the SOLE scope/view contract on every render_slice_modal / render_slice_row / apply_slice_in_dock frame -- the only flat frame keys are the transport envelope (action / request_id / endpoint_id / row / max_prerender_chars). There is NO flat-scope-field fallback anywhere (client producers send blob-only; the server reads blob-only; the tab-side push receiver reconstructs the spec from the blob). BOTH origins are fully wired through this object: SERVER -- glimpse / lightning_rod / _glimpse_push_slice_to_modal build the spec (normalize_from_glimpse_args -> with_overrides(resolved scope)) and derive render_scope FROM it via the ONE bridge _compose_render_scope_from_spec (whose sole delegate is the scalar _compose_render_scope -- the single render_scope birthplace); _handle_render_slice_modal builds ONE incoming_spec (normalize_from_modal_frame) and derives every scope/view local from it. CLIENT -- requestSliceModalRender / requestSliceRowRender send frame.render_spec only; dockedSliceContext.render_spec is the SOLE client scope handle (adoptRenderScope -> normalizeRenderScopeToSpec), reused verbatim by Refresh + per-row re-render. So this object is the one and only cache-render request contract on BOTH origins.,
# ]
@dataclass(frozen=True)
class RenderProjectionSpec:
    """One normalized, immutable cache-render request.

    Every cache-projected render (raw root dump, slice, slice+override,
    Lightning Rod) normalizes to an instance of this before projection/render.
    Only OVERRIDE fields are supplied at external boundaries; everything else
    takes the safe defaults below (the whole reason the object exists — so no
    caller omits a default and each downstream site guesses differently).

    Field groups mirror the HANDOFF spec:

    SOURCE / SCOPE IDENTITY
      * ``kind``       — which of the four scope cells this is.
      * ``root_uuid``  — the projection scope UUID (bare root, or the override
                         root when ``kind='slice_override'``). ``None`` for a
                         plain slice at its group's stored root.
      * ``group_id`` / ``group_name`` — the slice group (slice/slice_override).
      * ``lightning``  — the OPAQUE Lightning Rod selection dict (only for
                         ``kind='lightning_rod'``). Kept opaque on purpose:
                         it is about WHAT matches; traversal flags never go in
                         here. (It may also carry a mode-specific operation
                         policy -- e.g. render_tags ``postfix_policy`` --
                         which is still SELECTION/request identity, never a
                         traversal flag.)

    SELECTION / TRAVERSAL KNOBS
      * ``match_scope``       — SELECTION predicate scope (name vs name+note).
      * ``descend_policy``    — TRAVERSAL boundary policy (Phase-1).
      * ``exclude_completed`` — TRAVERSAL admission policy (Phase-1).
      * ``recursive``         — TRAVERSAL depth policy ([ap:EPR86W] part a).

    VIEW KNOBS
      * ``sort_by`` / ``sort_direction`` / ``since`` / ``headings_only`` /
        ``ancestor_notes``.
        ``sort_by`` is a REQUEST field: ``None`` may temporarily mean
        "inherit the slice group's saved default" for slice/slice_override
        specs only; the string ``'none'`` is the resolved sentinel for explicit
        document order. ``render_scope`` is the RESOLVED echo boundary and must
        never receive an unresolved slice ``None``.
        ``ancestor_notes`` (U6, default ``False``) is the teaching opt-out of
        the ancestor-note suppression: chronological renders (a created/
        modified sort in either direction, or ANY ``since`` -- window/
        curated, or an item-count tail since CF-13) suppress every non-leaf's
        note body (heading-only ancestors), and a LENS render additionally
        suppresses the connective scaffold above the input roots even in
        document order ([ap:I506WY]); ``True`` restores both for that
        request. It round-trips through the wire blob so
        frozen/scoped re-projections keep it; it is deliberately NOT part of
        the ``render_scope`` echo (the echo stays byte-identical, §10).

    OUTPUT / RENDER CONTRACT
      * ``as_markdown``     — output FORMAT (Markdown pseudo-doc vs bracketed
                              preview_tree). Orthogonal to the source axis.
      * ``content_format``  — how the ONE rendered string is painted
                              ('markdown' | 'raw_text').
      * ``tree_mode``       — how the top-half list is populated
                              ('render_tree' | 'root_only').
    """

    # --- source / scope identity ---
    kind: Kind = "raw_root"
    root_uuid: str | None = None
    group_id: str | None = None
    group_name: str | None = None
    lightning: dict[str, Any] | None = None

    # --- render FOCUS (a render primitive, NOT transport) ---
    #
    # The occurrence-exact identity of the ONE row a focused render targets, a
    # first-class, opaque render-spec member. A ``focus`` is about WHICH node
    # of the projected document to render (the modal-root->N breadcrumb + N's
    # subtree, option-Y), so it is part of the render REQUEST identity exactly
    # like ``root_uuid`` / ``lightning`` -- and it round-trips through the
    # opaque wire blob so a focused render is reproducible and reusable (the
    # agent-facing "GLIMPSE what the modal is showing right now" capability).
    #
    # Shape (canonicalized by ``_canonicalize_focus``): a dict
    # ``{"mode": "row", "uuid": str, "ancestor_path": [str, ...],
    #    "occurrence": int}`` -- ``mode`` is ``"row"`` for now (the only kind);
    # ``uuid`` + ``ancestor_path`` + ``occurrence`` are the SAME identity
    # TRIPLE ``_iter_markdown_focus_rows`` emits and the client echoes on a row
    # click, so it resolves occurrence-exactly against the projected tree.
    # ``None`` (the default) = the FULL document (no focus) -- byte-for-byte
    # the unfocused render. Kept OPAQUE like ``lightning``: it is about WHAT to
    # focus, never a traversal flag.
    focus: dict[str, Any] | None = None

    # --- projection / selection knobs ---
    match_scope: MatchScope = "name_and_note"
    descend_policy: DescendPolicy = "all"
    exclude_completed: bool = SERVER_SIDE_GLIMPSE_EXCLUDE_COMPLETED
    # [ap:EPR86W] part a — the depth leg of the traversal pair, named and
    # valued exactly like dock.peek's `recursive`. True (the default, the
    # historical behavior) = the ordinary unbounded walk. False = render the
    # scope root's own heading and note plus each DIRECT child's heading and
    # note, and nothing deeper -- child Docks and plain nodes alike
    # (walked as max_depth=1 through the ONE shared Phase-1 walker);
    # `descend_policy` is then moot, exactly as on peek. A TRAVERSAL knob,
    # never request identity: it rides to_render_spec_blob (an old blob
    # without the key deserializes to True -- frozen modals re-project
    # safely) and echoes in to_render_scope_fields beside `descend_policy`
    # (the client stored spec must reconstruct the same traversal on a
    # Refresh / per-row re-projection, or a depth-capped dock would repaint
    # full-depth). ONLY the raw-root surfaces honour it today (dock.render,
    # glimpse.run root_handle, the estate {dock} form, and the push/modal
    # path that re-projects their spec); a MEMBERSHIP selection (slice /
    # slice_override / lightning_rod) is unbounded by construction, so the
    # ONE normalizer refuses recursive=False for those kinds LOUDLY rather
    # than silently ignoring it.
    recursive: bool = True

    # --- view knobs ---
    sort_by: str | None = "none"  # None (slice request inherit) | 'none' | 'created' | 'modified'
    sort_direction: str = "asc"
    # [ap:TVIIBN] 4d(a) recency BASIS for an item-count 'newest' selection
    # ('last N'/'newest N'): 'created' | 'modified', or None (the default) =
    # resolve at apply time (resolve_item_count_basis): the presentation
    # sort_by axis when it is a timestamp, else 'modified'. A VIEW knob: never
    # request identity; rides to_render_spec_blob (an old blob without the key
    # deserializes to None -- the resolve-at-apply rule) and echoes in
    # to_render_scope_fields beside `since` (it co-decides WHAT was selected).
    recent_by: str | None = None
    since: SinceSpec | None = None
    headings_only: bool = False
    # SHORT/ FULL heading ids ([ap:0M6VP8] part a): False (the default) --
    # the rule ships -- prints each headings-only [id] as the node's short
    # resolver-compatible handle (8 hex, lengthened just enough when the
    # 8-hex prefix is ambiguous across the pinned cache). True is the sole
    # opt-in: print the full 36-char UUID. A VIEW knob: never request
    # identity (it rides to_render_spec_blob so pushes/frozen specs
    # round-trip it; deliberately NOT in to_render_scope_fields, mirroring
    # ancestor_notes, so the render_scope echo stays byte-identical).
    full_uuids: bool = False
    # REPOSITORY VIEW (U1, MasterSpec §5.1): the clean-Markdown commit view
    # for beacon-slice renders (MasterSpec [ap:YGYP2O]). False (the default)
    # is today's render byte-for-byte. True projects the Stage-2 forest in
    # front of the ONE composer (§4's format: repository-relative paths,
    # kind + signature headings, labels + comments, no Workflowy trace) and
    # gates a few emission sites inside it. A VIEW knob: never request
    # identity -- deliberately NOT emitted by to_render_spec_blob, NOT read
    # by from_render_spec_blob, NOT in to_render_scope_fields (§3.6), so
    # every blob and every render_scope echo stays byte-identical. Set ONLY
    # by the one normalizer (explicit args, or the tri-state
    # with_view_overrides channel), which also owns the §3.4 backstop guard.
    repository_view: bool = False
    # U6 (ap:E03BN3) TEACHING FLAG — default False = the rule ships: on a
    # CHRONOLOGICAL render (render_is_chronological: a resolved created/modified
    # sort in either direction, or ANY since — a window/curated on the
    # SinceSpec's own axis, or an item-count tail since CF-13 — 'newest N'/
    # 'last N' and the positional 'bottom N' alike; the excerpt order stays
    # document order unless the caller sorts) every non-leaf of the final
    # render forest (root included when non-leaf) renders HEADING-ONLY; leaf
    # notes print. On a LENS render the connective scaffold above the input
    # roots is additionally suppressed even in document order ([ap:I506WY]).
    # Passing True is the sole opt-out: the materializer skips the
    # chronological-ancestor cause entirely (and the producer folds the flag
    # off the lens-scaffold cause too), restoring the ancestor-notes behavior
    # for that call. It exists so the schema itself teaches that ancestor
    # notes are a choice. There is deliberately NO way to suppress ancestor
    # notes in DOCUMENT order outside the lens-scaffold rule (Dan's rule
    # doesn't ask for one). A VIEW knob: never request identity, never a
    # `since` token.
    ancestor_notes: bool = False

    # --- output / render contract ---
    as_markdown: bool = True
    content_format: str = "markdown"
    tree_mode: str = "render_tree"

    # ---------------------------------------------------------------- helpers

    def sort_by_internal(self) -> str | None:
        """Return the internal ``sort_by`` form: ``None`` for the wire 'none'.

        The internal projection pipeline uses ``sort_by=None`` for natural
        order; the wire / ``render_scope`` form uses the string ``'none'``.
        This is the ONE translation point so callers never re-decide it.
        """
        return self.sort_by if self.sort_by in ("created", "modified") else None

    @property
    def is_raw_dump(self) -> bool:
        """True for a bare root_uuid raw dump (no group, no membership)."""
        return self.kind == "raw_root"

    @property
    def root_overridden(self) -> bool:
        """True when a group's tags/policy are scoped to an override root."""
        return self.kind == "slice_override"

    @property
    def is_lightning(self) -> bool:
        return self.kind == "lightning_rod"

    # @beacon[
    #   id=auto-beacon@RenderProjectionSpec.with_overrides-5b1j,
    #   role=RenderProjectionSpec.with_overrides,
    #   slice_labels=glimpse-render,
    #   kind=ast,
    # ]
    def with_overrides(self, **changes: Any) -> "RenderProjectionSpec":
        """RAW, INTERNAL field replacement (frozen-safe ``dataclasses.replace``).

        ⚠ TWO-TIER MUTATION CONTRACT — read before using this. There are
        exactly two ways to derive a new spec from an existing one, and they
        are NOT interchangeable:

          * :meth:`with_view_overrides` — the NORMALIZED, boundary/agent
            override path. Use it whenever an EXTERNAL caller (the ``glimpse``
            tool applying agent-supplied view knobs onto a tab-sourced spec)
            overlays fields. It re-runs the ONE normalizer, so values are
            coerced and ``kind`` is re-decided-in-the-one-place. This is the
            path that keeps the "construction always normalizes" invariant.

          * ``with_overrides`` (THIS method) — RAW ``replace``: NO coercion, NO
            ``kind`` re-derivation. Reserved for INTERNAL writes of fields that
            are ALREADY canonical AND where preserving the current ``kind`` is
            INTENTIONAL — specifically resolving a scope's ``root_uuid`` to its
            canonical cache UUID (``_grace_resolve_spec_uuids``; the several
            ``with_overrides(root_uuid=resolved)`` sites) and swapping in a
            ``_canonicalize_focus``-cleaned ``focus``. Writing ``group_id`` /
            ``root_uuid`` presence-changing values through here on purpose
            keeps a RESOLVED plain ``slice`` (which carries both a group_id and
            a resolved root_uuid) from being re-derived as ``slice_override``.

        DO NOT use ``with_overrides`` to apply agent/boundary VIEW overrides
        (as_markdown / headings_only / sort_by / sort_direction / since) — that
        would skip normalization. Use :meth:`with_view_overrides` for those.
        """
        return replace(self, **changes)

    def with_view_overrides(
        self,
        *,
        as_markdown: bool | None = None,
        headings_only: bool | None = None,
        sort_by: str | None = None,
        sort_direction: str | None = None,
        since: SinceSpec | None = None,
        recent_by: str | None = None,
        repository_view: bool | None = None,
    ) -> "RenderProjectionSpec":
        """Overlay agent-supplied VIEW knobs onto this spec, RE-NORMALIZED.

        The single, dedicated NORMALIZED override mutator. It exists so an
        external caller can layer explicit view overrides onto an already-built
        spec (the motivating case: the ``render_spec="modal"`` path overlays
        the agent's tri-state ``as_markdown`` / ``headings_only`` / ``sort_by``
        / ``since`` onto the spec the docked modal reported) WITHOUT the two
        cracks a raw :meth:`with_overrides` would open:

          1. it re-runs the ONE normalizer (``normalize_from_glimpse_args``),
             so overridden values are coerced exactly as at ingress (a bad
             ``sort_direction`` clamps to ``asc``, a non-created/modified
             ``sort_by`` collapses to the wire ``'none'``, etc.); and
          2. it PRESERVES this spec's resolved scope identity by passing the
             current ``kind`` as ``kind_hint`` — so re-normalization never
             silently reclassifies a resolved ``slice`` as ``slice_override``.

        TRI-STATE: every parameter defaults to ``None`` meaning "INHERIT this
        spec's current value" (the modal-view default: show the agent what the
        human is looking at). A non-``None`` value is an EXPLICIT override. So
        ``headings_only=None`` keeps whatever the modal had, ``headings_only=
        True`` forces titles-only, and ``headings_only=False`` forces bodies
        back on even if the modal was in titles-only mode — the symmetric
        override the agent needs.

        This mutates ONLY the VIEW/output knobs. Scope identity (``kind`` /
        ``group_id`` / ``group_name`` / ``root_uuid`` / ``lightning`` /
        ``focus``), the SELECTION/TRAVERSAL knobs (``match_scope`` /
        ``descend_policy`` / ``exclude_completed``), and the ``content_format`` /
        ``tree_mode`` output contract are carried through unchanged. It does
        NOT run the LOUD combination guards (e.g. sort-with-focus) — those run
        ONCE, separately, on the final merged spec at the tool boundary.
        """
        return RenderProjectionSpec.normalize_from_glimpse_args(
            group_id=self.group_id,
            group_name=self.group_name,
            root_uuid=self.root_uuid,
            lightning=self.lightning,
            focus=self.focus,
            match_scope=self.match_scope,
            descend_policy=self.descend_policy,
            exclude_completed=self.exclude_completed,
            # [ap:EPR86W] part a: recursive is a TRAVERSAL knob carried
            # through unchanged (like descend_policy), never a tri-state
            # agent-view override -- a view overlay never silently re-decides
            # the depth of the walk.
            recursive=self.recursive,
            # VIEW knobs: None => inherit this spec's current value.
            sort_by=(self.sort_by if sort_by is None else sort_by),
            sort_direction=(
                self.sort_direction if sort_direction is None else sort_direction
            ),
            since=(self.since if since is None else since),
            # [ap:TVIIBN] 4d(a): the recency basis rides the same tri-state
            # override channel as since (None = inherit; a value is explicit).
            recent_by=(self.recent_by if recent_by is None else recent_by),
            headings_only=(
                self.headings_only if headings_only is None else bool(headings_only)
            ),
            # U6 ancestor_notes is NOT one of the tri-state agent-override
            # knobs; it is CARRIED THROUGH unchanged (like content_format /
            # tree_mode) so a blob-sourced opt-out survives view overrides and
            # an override never silently re-decides the teaching flag.
            ancestor_notes=self.ancestor_notes,
    # [ap:0M6VP8] full_uuids is likewise CARRIED THROUGH unchanged:
            # an override never silently flips the heading-id form.
            full_uuids=self.full_uuids,
            # REPOSITORY VIEW (U1, MasterSpec §3.4/§5.2): a tri-state override
            # beside headings_only -- None = INHERIT this spec's current value;
            # True/False is an EXPLICIT call-level override. Re-normalization
            # runs the ONE normalizer, whose repository_view backstop guard
            # (§3.4) re-checks the MERGED spec -- exactly the catch for
            # blob-sourced specs (formal JSON, estate entries) the boundary
            # helper cannot see.
            repository_view=(
                self.repository_view if repository_view is None else bool(repository_view)
            ),
            as_markdown=(
                self.as_markdown if as_markdown is None else bool(as_markdown)
            ),
            content_format=self.content_format,
            tree_mode=self.tree_mode,
            # Preserve resolved scope identity: re-normalization must not
            # reclassify a resolved slice (group_id + resolved root_uuid) as
            # slice_override. The one normalizer honors a valid hint.
            kind_hint=self.kind,
        )

    # @beacon[
    #   id=auto-beacon@RenderProjectionSpec.to_render_scope_fields-bu8v,
    #   role=RenderProjectionSpec.to_render_scope_fields,
    #   slice_labels=glimpse-render,
    #   kind=ast,
    # ]
    def to_render_scope_fields(self) -> dict[str, Any]:
        """Serialize the NORMALIZED, non-selection fields for ``render_scope``.

        The scope-echo half of the render_scope bridge: ``_compose_render_scope``
        adopts these keys (notably ``descend_policy``). The opaque ``selection``
        / derived display-only fields for Lightning Rod are composed by
        ``_compose_render_scope`` itself (it owns that), so this helper
        deliberately does NOT emit ``selection`` — it carries only the
        normalized scope-echo knobs. (Distinct from :meth:`to_render_spec_blob`,
        which serializes the FULL spec, incl. ``selection``, for the wire.)
        """
        return {
            "kind": self.kind,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "root_uuid": self.root_uuid or None,
            "match_scope": self.match_scope,
            "descend_policy": self.descend_policy,
            # [ap:EPR86W] part a: echoed beside `descend_policy` (the other
            # traversal leg) so the client's stored spec reconstructs the SAME
            # depth on a Refresh / per-row re-projection; default True.
            "recursive": bool(self.recursive),
            "sort_by": self.sort_by if self.sort_by in ("created", "modified") else "none",
            "sort_direction": self.sort_direction if self.sort_direction in ("asc", "desc") else "asc",
            "since": since_to_wire(self.since),
            # [ap:TVIIBN] 4d(a): echoed beside `since` (it co-decides WHAT was
            # selected); null = the resolve-at-apply rule (sort axis, else
            # modified).
            "recent_by": self.recent_by if self.recent_by in ("created", "modified") else None,
            "headings_only": bool(self.headings_only),
            "content_format": self.content_format if self.content_format in CONTENT_FORMATS else "markdown",
            "tree_mode": self.tree_mode if self.tree_mode in TREE_MODES else "render_tree",
        }

    # @beacon[
    #   id=auto-beacon@RenderProjectionSpec.to_render_spec_blob-ql9t,
    #   role=RenderProjectionSpec.to_render_spec_blob,
    #   slice_labels=glimpse-render,
    #   kind=ast,
    # ]
    def to_render_spec_blob(self) -> dict[str, Any]:
        """Serialize the FULL spec as the nested ``render_spec`` wire blob.

        The OPAQUE-WIRE serializer -- the round-trip partner of
        :meth:`from_render_spec_blob`. Unlike :meth:`to_render_scope_fields`
        (which serializes only the flat NON-selection knobs for the
        ``render_scope`` ECHO and deliberately omits ``selection``/``lightning``
        because the composer owns that), THIS emits every field a frame needs
        to reconstruct the request spec: the scope identity (``kind`` /
        ``group_id`` / ``group_name`` / ``root_uuid`` / the OPAQUE ``lightning``
        selection), the selection/traversal knobs (``match_scope`` /
        ``descend_policy`` / ``recursive`` [ap:EPR86W] part a),         the view
        knobs (``sort_by`` [wire ``'none'``] /
        ``sort_direction`` / ``since`` object-or-null / ``headings_only`` /
        ``ancestor_notes``), and the output
        contract (``as_markdown`` / ``content_format`` / ``tree_mode``).
        ``from_render_spec_blob`` re-derives ``kind`` from the truth table, so
        emitting it here is redundant-but-honest (a debugging aid); the
        round-trip is defined by the truth-table inputs, not the echoed kind.
        Pure; no I/O.
        """
        return {
            "kind": self.kind,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "root_uuid": self.root_uuid or None,
            "lightning": self.lightning,
            "focus": self.focus,
            "match_scope": self.match_scope,
            "descend_policy": self.descend_policy,
            # [ap:EPR86W] part a: the depth leg of the traversal pair rides
            # the blob so pushes / frozen specs round-trip it (an old blob
            # WITHOUT the key deserializes to True -- the default -- so frozen
            # modals re-project safely). Also echoed in to_render_scope_fields
            # beside descend_policy: like that knob, losing it on a client
            # re-projection would repaint a depth-capped dock full-depth.
            "recursive": bool(self.recursive),
            "sort_by": (self.sort_by if self.sort_by in ("none", "created", "modified") else None),
            "sort_direction": self.sort_direction if self.sort_direction in ("asc", "desc") else "asc",
            "since": since_to_wire(self.since),
            # [ap:TVIIBN] 4d(a): the recency basis rides the blob so pushes /
            # frozen specs round-trip it (an old blob without the key
            # deserializes to None -- the resolve-at-apply rule). Also echoed
            # in to_render_scope_fields (unlike ancestor_notes/full_uuids:
            # like `since`, it co-decides WHAT was selected).
            "recent_by": self.recent_by if self.recent_by in ("created", "modified") else None,
            "headings_only": bool(self.headings_only),
            # U6 teaching flag: rides the wire blob so frozen/scoped
            # re-projections round-trip it (an old blob WITHOUT the key
            # deserializes to False — the rule's default — so frozen modals
            # re-project safely). Deliberately NOT in to_render_scope_fields:
            # the render_scope echo stays byte-identical (§10).
            "ancestor_notes": bool(self.ancestor_notes),
            # [ap:0M6VP8] the full_uuids opt-in: same wire discipline as
            # ancestor_notes (round-trips the blob; never in the scope echo).
            "full_uuids": bool(self.full_uuids),
            "as_markdown": bool(self.as_markdown),
            "content_format": self.content_format if self.content_format in CONTENT_FORMATS else "markdown",
            "tree_mode": self.tree_mode if self.tree_mode in TREE_MODES else "render_tree",
        }

    # ------------------------------------------------------------ normalizers

    @classmethod
    # @beacon[
    #   id=auto-beacon@RenderProjectionSpec.normalize_from_glimpse_args-d2ep,
    #   role=RenderProjectionSpec.normalize_from_glimpse_args,
    #   slice_labels=glimpse-render,
    #   kind=ast,
    # ]
    def normalize_from_glimpse_args(
        cls,
        *,
        slice_group: str | None = None,
        group_id: str | None = None,
        group_name: str | None = None,
        root_uuid: str | None = None,
        lightning: dict[str, Any] | None = None,
        focus: dict[str, Any] | None = None,
        match_scope: str = "name_and_note",
        descend_policy: str = "all",
        exclude_completed: bool | None = None,
        recursive: bool = True,
        sort_by: str | None = None,
        sort_direction: str = "asc",
        since: SinceSpec | None = None,
        recent_by: str | None = None,
        headings_only: bool = False,
        ancestor_notes: bool = False,
        full_uuids: bool = False,
        repository_view: bool = False,
        as_markdown: bool = True,
        content_format: str = "markdown",
        tree_mode: str = "render_tree",
        kind_hint: str | None = None,
    ) -> "RenderProjectionSpec":
        """Normalize the ``glimpse`` / ``lightning_rod`` public-arg surface.

        The ONE normalizer every render origin routes through (the modal-frame
        and blob deserializers delegate here too), and the SOLE place ``kind``
        is ever decided. Encodes the (slice_group, root_uuid, lightning) truth
        table into ``kind``:

          * ``lightning`` present            -> ``lightning_rod``.
          * ``slice_group`` + ``root_uuid``  -> ``slice_override``.
          * ``slice_group`` only             -> ``slice``.
          * ``root_uuid`` only / neither     -> ``raw_root``.

        ``kind_hint``: when a caller carries an EXPLICIT, already-decided
        ``kind`` (namely :meth:`from_render_spec_blob`, deserializing a wire
        blob whose producer -- the JS ``normalizeRenderScopeToSpec`` / the
        server ``to_render_spec_blob`` -- already recorded the resolved kind),
        pass it here. A VALID hint (one of :data:`SPEC_KINDS`) is HONORED
        verbatim; an absent/invalid hint falls back to the presence truth table
        above. This keeps the JS and Python sides in agreement: both preserve a
        valid ``scope.kind``, so a RESOLVED plain ``slice`` that (for
        echo/reuse) carries BOTH ``group_id`` and a resolved ``root_uuid`` is
        not silently RE-DERIVED as ``slice_override`` on a round-trip. The
        decision still happens HERE, inside the one normalizer -- callers NEVER
        post-mutate ``kind`` -- so there remains exactly one authority for
        ``kind``. The presence-derivation is the safe default; the hint only
        ever preserves a producer's already-correct resolved kind.

        Validation is lenient-with-safe-fallback for the knobs (an unknown
        value falls back to its default) — the LOUD guards that reject bad
        combinations (e.g. ``match_scope='name'`` without a slice) live at the
        ``glimpse`` tool boundary, not here; this normalizer assumes
        already-guarded inputs. THE ONE EXCEPTION is the traversal pair's
        depth leg ([ap:EPR86W] part a): ``recursive=False`` on a membership
        kind (slice / slice_override / lightning_rod) raises ValueError here,
        because every ingress (MCP args, pasted blobs, modal frames) routes
        through this one normalizer and a silently-ignored traversal request
        is the exact failure mode the to-do forbids.
        """
        _group_id = group_id if group_id is not None else (slice_group or None)

        # ``kind`` is decided ONCE, here. Prefer a valid explicit hint (a
        # producer's already-resolved kind, e.g. from a wire blob); else derive
        # from the (lightning > group+root > group > raw_root) presence table.
        _hint = str(kind_hint or "").strip().lower()
        if _hint in SPEC_KINDS:
            kind: Kind = _hint  # type: ignore[assignment]
        elif lightning is not None:
            kind = "lightning_rod"
        elif _group_id and root_uuid:
            kind = "slice_override"
        elif _group_id:
            kind = "slice"
        else:
            kind = "raw_root"

        ms = str(match_scope or "name_and_note").strip().lower()
        if ms not in MATCH_SCOPES:
            ms = "name_and_note"

        dp = str(descend_policy or "all").strip().lower()
        # Public spelling alias only; absent/unknown values keep the old
        # default, and the normalized wire vocabulary stays unchanged.
        if dp == "stop_at_child_docks":
            dp = "stop_at_child_anchors"
        if dp not in DESCEND_POLICIES:
            dp = "all"

        # [ap:EPR86W] part a — THE loud guard for the depth leg of the
        # traversal pair. A MEMBERSHIP selection (slice, slice_override,
        # lightning_rod) is defined by its predicate over the WHOLE scope; a
        # depth cap would be a new semantic (which members survive a cap?)
        # nobody asked for, so `recursive=False` on those kinds is refused
        # here -- once, for EVERY ingress that routes through this one
        # normalizer (MCP args, pasted blobs, modal frames) -- rather than
        # silently ignored downstream. Only the raw-root surfaces honour the
        # cap (dock.render, glimpse.run root_handle, the estate {dock} form,
        # and the push/modal path that re-projects their spec); unit 4b adds
        # the pair to the other walking verbs' schemas with the same refusal
        # spelled out in their describes. Raising (not a safe fallback) is
        # deliberate: a silently-ignored traversal request is the exact
        # failure mode the to-do forbids.
        _recursive = bool(recursive)
        if not _recursive and kind in ("slice", "slice_override", "lightning_rod"):
            raise ValueError(
                "recursive=False (render this scope's root + direct children "
                "only) is a RAW-ROOT traversal policy: dock.render, "
                "glimpse.run with root_handle, and the estate {dock} form "
                "honour it. A slice-group or Lightning Rod selection is "
                "defined by its predicate over the whole scope, so a depth "
                "cap would silently change membership; omit recursive (or "
                "pass true) for that scope."
            )

        # REPOSITORY VIEW (U1, MasterSpec §3.4) — the backstop guard. Every
        # ingress (MCP args, pasted blobs, modal frames) routes through this
        # ONE normalizer, so a repository_view request that the boundary
        # helper cannot see (a formal render-spec JSON, an estate entry,
        # with_view_overrides onto a saved spec) is still refused LOUDLY here
        # rather than silently rendering a wrong or unstable document. The
        # view is a stable, clean, commit-ready document: a bracketed output
        # format, a row focus, full-UUID heading ids, a chronological sort
        # and a `since` prune would each reorder, decorate or trim it. The
        # established raising mechanism (the recursive=False guard above);
        # "loud, never silent" (A7).
        _repository_view = bool(repository_view)
        if _repository_view:
            if not bool(as_markdown):
                raise ValueError(
                    "repository_view=true renders the clean Markdown commit "
                    "document only: as_markdown=false (the bracketed full/UUID "
                    "format) carries exactly the Workflowy trace the view "
                    "exists to remove. Drop as_markdown=false (the Markdown "
                    "format is the default)."
                )
            if focus is not None:
                raise ValueError(
                    "repository_view=true cannot combine with a saved focus "
                    "(a row/rows focus renders one occurrence's breadcrumb "
                    "view, which is neither the whole slice nor a stable "
                    "document). Clear the focus and rerun."
                )
            if bool(full_uuids):
                raise ValueError(
                    "repository_view=true removes every node handle from the "
                    "document, so full_uuids=true (full UUID [id] tokens) has "
                    "nothing to decorate. Drop full_uuids."
                )
            if isinstance(sort_by, str) and sort_by.strip().lower() in (
                "created",
                "modified",
            ):
                raise ValueError(
                    "repository_view=true fixes the document order (file "
                    "paths, then source position), so sort_by='created'|"
                    "'modified' would break the stable order a committed "
                    "slice needs. Drop sort_by (document order is forced)."
                )
            if since is not None:
                raise ValueError(
                    "repository_view=true renders the whole selection (a "
                    "stable document), so a since/recent trim (window, "
                    "'curated', an item count) cannot combine with it. Drop "
                    "recent."
                )
            if (
                isinstance(lightning, dict)
                and str((lightning.get("mode") or "")).strip().lower()
                == "gather"
            ):
                # Chain 1 r1 (W1/F1): a formal gather spec can never produce
                # the clean document -- the gather outline is the Workflowy
                # #tag vocabulary, and the tag outline formatter runs before
                # the Stage-2 tail the view projects. Refuse the combination
                # here so every blob-sourced route (an estate entry, a pasted
                # spec) fails loudly instead of returning the outline as the
                # view's document. Refusal of an incompatible call, never an
                # addition of the view to gather.
                raise ValueError(
                    "repository_view=true renders a beacon slice's clean "
                    "Markdown commit document; a Lightning gather selection "
                    "is the Workflowy #tag vocabulary outline, which carries "
                    "exactly the tag syntax the view removes. Drop "
                    "repository_view for the gather, or select a tags/"
                    "symbols slice for the commit file."
                )

        # Sort is kind-aware. Python None / JSON null is meaningful only for
        # slice-associated request specs, where it means "inherit the group's
        # saved default sort" at the slice resolve-point. The string 'none' is
        # a resolved sentinel: explicit document order, do NOT inherit.
        if sort_by is None:
            sb = None if kind in ("slice", "slice_override") else "none"
        elif isinstance(sort_by, str):
            sb_raw = sort_by.strip().lower()
            sb = sb_raw if sb_raw in ("none", "created", "modified") else "none"
        else:
            sb = "none"

        sd = str(sort_direction or "asc").strip().lower()
        if sd not in SORT_DIRECTIONS:
            sd = "asc"

        # [ap:TVIIBN] 4d(a): the recency basis for a 'newest' item_count
        # selection. Lenient like the other knobs: anything but the two axes
        # (including None) degrades to None = resolve-at-apply
        # (resolve_item_count_basis: the sort axis when it is a timestamp,
        # else 'modified').
        rb = str(recent_by or "").strip().lower()
        recent_by_norm = rb if rb in ("created", "modified") else None

        cf = str(content_format or "markdown").strip().lower()
        if cf not in CONTENT_FORMATS:
            cf = "markdown"

        tm = str(tree_mode or "render_tree").strip().lower()
        if tm not in TREE_MODES:
            tm = "render_tree"

        if since is not None and not isinstance(since, SinceSpec):
            raise TypeError("RenderProjectionSpec.since must be SinceSpec or None; parse edge strings before normalizing")

        return cls(
            kind=kind,
            root_uuid=(root_uuid or None),
            group_id=_group_id,
            group_name=group_name,
            lightning=lightning,
            focus=_canonicalize_focus(focus),
            match_scope=ms,  # type: ignore[arg-type]
            descend_policy=dp,  # type: ignore[arg-type]
            exclude_completed=(
                SERVER_SIDE_GLIMPSE_EXCLUDE_COMPLETED
                if exclude_completed is None
                else bool(exclude_completed)
            ),
            # [ap:EPR86W] part a: the depth leg of the traversal pair.
            recursive=_recursive,
            sort_by=sb,
            sort_direction=sd,
            since=since,
            recent_by=recent_by_norm,
            headings_only=bool(headings_only),
            ancestor_notes=bool(ancestor_notes),
            # [ap:0M6VP8]: the full_uuids opt-in rides the one normalizer
            # (the blob deserializer forwards its key here).
            full_uuids=bool(full_uuids),
            # REPOSITORY VIEW (U1): set ONLY here (§3.6 — never on the blob
            # or the scope echo); the §3.4 guard above already ran.
            repository_view=_repository_view,
            as_markdown=bool(as_markdown),
            content_format=cf,
            tree_mode=tm,
        )

    @classmethod
    # @beacon[
    #   id=auto-beacon@RenderProjectionSpec.from_render_spec_blob-j5zl,
    #   role=RenderProjectionSpec.from_render_spec_blob,
    #   slice_labels=glimpse-render,
    #   kind=ast,
    # ]
    def from_render_spec_blob(cls, blob: dict[str, Any]) -> "RenderProjectionSpec":
        """Deserialize a nested ``render_spec`` wire blob into a spec.

        The OPAQUE-WIRE deserializer: an Apply/push frame carries the whole
        request spec as ONE nested ``render_spec`` object. Its keys are the
        same spec fields ``normalize_from_glimpse_args`` accepts, so we route
        them straight through the ONE normalizer to coerce/default every knob
        identically to every other entry point. The opaque ``lightning``
        selection is preserved verbatim (only shape-checked to a dict). This
        keeps the deserializer a thin adapter over the single normalizer. An
        empty/``None`` blob yields the all-default spec (``raw_root`` with
        ``root_uuid=None``).

        ``kind`` PRESERVATION: the blob's own ``kind`` (recorded by its
        producer -- ``to_render_spec_blob`` server-side,
        ``normalizeRenderScopeToSpec`` client-side, both of which preserve a
        valid kind) is forwarded to the normalizer as ``kind_hint``. The
        normalizer honors a VALID hint and otherwise falls back to the presence
        truth table -- so a RESOLVED plain ``slice`` carrying both a
        ``group_id`` and a resolved ``root_uuid`` round-trips as ``slice``
        rather than being silently promoted to ``slice_override``. The decision
        still lives ONLY in the normalizer; we merely pass the producer's
        already-decided kind through, never post-mutate it here.
        """
        b = blob or {}
        lightning = b.get("lightning")
        if not isinstance(lightning, dict):
            lightning = None
        focus = b.get("focus")
        if not isinstance(focus, dict):
            focus = None
        return cls.normalize_from_glimpse_args(
            group_id=(b.get("group_id") or None),
            group_name=(b.get("group_name") or None),
            root_uuid=(b.get("root_uuid") or None),
            lightning=lightning,
            focus=focus,
            match_scope=str(b.get("match_scope") or "name_and_note"),
            descend_policy=str(b.get("descend_policy") or "all"),
            # [ap:EPR86W] part a: an old blob without the key deserializes to
            # True (the unbounded default) -- frozen modals re-project safely.
            recursive=bool(b.get("recursive", True)),
            sort_by=b.get("sort_by"),
            sort_direction=str(b.get("sort_direction") or "asc"),
            since=since_from_wire(b.get("since")),
            # [ap:TVIIBN] 4d(a): an old blob without the key deserializes to
            # None -- the resolve-at-apply recency basis (frozen modals
            # re-project safely).
            recent_by=(b.get("recent_by") or None),
            headings_only=bool(b.get("headings_only") or False),
            # U6: an old blob without the key deserializes to False (the
            # rule's default) — frozen modals re-project safely (§10).
            ancestor_notes=bool(b.get("ancestor_notes") or False),
            # [ap:0M6VP8]: an old blob without the key deserializes to False
            # (short heading ids are the default) — frozen modals re-project
            # safely.
            full_uuids=bool(b.get("full_uuids") or False),
            as_markdown=bool(b.get("as_markdown", True)),
            content_format=str(b.get("content_format") or "markdown"),
            tree_mode=str(b.get("tree_mode") or "render_tree"),
            kind_hint=(b.get("kind") or None),
        )

    @classmethod
    # @beacon[
    #   id=auto-beacon@RenderProjectionSpec.normalize_from_modal_frame-qdjz,
    #   role=RenderProjectionSpec.normalize_from_modal_frame,
    #   slice_labels=glimpse-render,
    #   kind=ast,
    # ]
    def normalize_from_modal_frame(cls, data: dict[str, Any] | None) -> "RenderProjectionSpec":
        """Normalize an incoming ``render_slice_modal`` / push frame dict.

        OPAQUE-WIRE: the whole request rides in ONE nested ``render_spec``
        object, deserialized via :meth:`from_render_spec_blob` (which routes
        through the ONE normalizer and re-derives ``kind`` from the truth
        table). ``render_spec`` is the SOLE scope/view contract on the wire --
        there are no scattered top-level scope fields to read; only the
        transport envelope (``action`` / ``request_id`` / ``endpoint_id`` /
        ``max_prerender_chars``) lives flat on the frame, and none of those are
        spec fields. (The clicked ROW is NO LONGER a flat envelope field: it is
        the render primitive ``render_spec.focus`` now -- see the ``focus``
        field.) A frame with a missing/empty ``render_spec``
        yields a default spec (its ``kind`` resolves to ``raw_root`` with
        ``root_uuid=None``); the caller's own scope validation then rejects it.
        """
        d = data or {}
        blob = d.get("render_spec")
        return cls.from_render_spec_blob(blob if isinstance(blob, dict) else {})
