# @beacon[
#   id=f8policy@module,
#   slice_labels=slices,
#   kind=ast,
#   comment=Pure server-side F8 slice-policy core: Phase 1 (simplified search
#     evaluator + minimal-subtree builder), Phase 0 (UUID-MEMBERSHIP-ONLY DOM-vs-cache
#     validator -- every visible UUID must exist in cache; NO parent_id edge or
#     name/note comparison, matching the proven docked-modal staleness contract;
#     fail-closed only on a genuinely-missing UUID), and Phase 2 (suppression/
#     deprecated/expand plan builder). No WebSocket / no DOM / no broker; plain
#     functions over plain dicts.
# ]
"""F8 slice-policy core algorithms (pure, server-side, transport-free).

This module is the SINGLE SOURCE OF TRUTH for the F8 "slice" decision logic.
It is deliberately a free-function module (no ``self``, no class) so it is
trivially unit-testable and so the same logic can be driven from either input
source without duplication:

  * **F8 / DOM-driven path** — the browser sends its currently-visible subtree;
    we validate it against the /nodes-export cache (Phase 0) and compute an
    expansion plan over exactly that subtree (Phase 2).
  * **No-DOM / server-driven "GLIMPSE this topic" path (future)** — we build the
    candidate subtree on the server straight from the cache (Phase 1) and feed
    the SAME subtree into Phase 2.

Because both paths converge on one tree → one Phase-2 plan, the client and the
headless projector can never drift. The client stays purely mechanical: it
receives ``{collapse_suppressed, collapse_deprecated, expand}`` UUID sets and
calls ``WF.collapseItem`` / ``WF.expandItem``; it owns NO policy.

================================================================================
v1 SEMANTICS — DELIBERATE. DO NOT RE-LITIGATE WITHOUT A CONCRETE COUNTEREXAMPLE.
================================================================================
These were settled in extended design discussion (Dan + agents). They are
intentional simplifications, not bugs:

1. MEMBERSHIP is a restricted boolean: a node is in the slice iff it carries at
   least one tag matching an active prefix AND no tag matching the deprecated
   variant of an active prefix. The bare slice tag automatically pulls in the
   auto-expand variant as a leaf (prefix match); the negation drops the
   deprecated variant. This mirrors Workflowy's own search semantics for
   ``#tag--a -#tag--a--deprecated`` so the cache evaluation equals what a human
   sees when they paste the compiled search string. (Only OR-of-prefixes minus
   deprecated-variants for now; no parentheses / no ``>`` descendant operator.)

2. PREFIX MATCH IS LITERAL ``startsWith`` on the '#'-stripped token — exactly
   mirroring the JS ``tagTokenMatchesExpandPrefix`` in hotkeys-shared.js /
   glimpse-cache-client.js. It is NOT segment-aware in the matcher itself; the
   "segment" feel comes from the user choosing prefixes that end at ``--``
   boundaries. Server and client MUST share this literal-prefix behavior or
   they drift. (See ``tag_matches_prefix``.)

3. DEPRECATED is a COLLAPSE op, not a leaf-removal op. F8 can collapse a
   deprecated subtree; it cannot make a deprecated *visible leaf* vanish from a
   manually-shaped DOM tree (collapse only hides descendants). True deprecated
   exclusion is the MEMBERSHIP/search layer's job (Phase 1 / the pasted search
   string). A deprecated node is NOT collapsed if doing so would hide a live
   (non-deprecated, in-slice) descendant — "don't collapse through live
   content" (Dan's rule). See ``contains_live_active_slice_hit``.

4. SUPPRESS (and global-never-expand) is also a COLLAPSE op. Suppression of an
   ancestor that lies ABOVE the slice-group root is INERT by construction,
   because Phase 2 only ever sees the passed subtree rooted at the group root —
   it never looks at ancestors of that root. (This is the "zoom inside a
   suppressed subtree to un-suppress it" usability feature.)

5. AUTO-EXPAND RUNS LAST and DOMINATES DOWNWARD. If an auto-expand node is an
   ANCESTOR of a collapse (suppress/deprecated) node, expansion wins and the
   whole subtree opens — matching what Workflowy's expand-all does (it reopens
   everything beneath, even previously hidden nodes). We therefore DROP any
   expand target that is a DESCENDANT of a collapse node (it would be hidden /
   unreachable), and KEEP any expand target that is an ANCESTOR of one. Making
   suppression dominate over an enclosing auto-expand was explicitly judged not
   worth the added complexity for v1.

6. CLIENT EXECUTION ORDER: collapse_suppressed, then collapse_deprecated, then
   expand. Because the server already pruned cross-set conflicts, the order is
   robust; "expand last" guarantees auto-expand's downward dominance.

Each operation set is normalized to TOPMOST-ONLY (if both an ancestor and a
descendant are candidates for the SAME op, keep the ancestor, drop the
descendant) so the client issues the minimal set of DOM calls and there are no
intra-set inconsistencies.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# Cache-name tag normalization (the DOM-vs-cache parity fix).
# ---------------------------------------------------------------------------
# The LIVE F8 path harvests tags from the DOM's .contentTag data-val (clean
# '#tag' tokens) via retagF8TreeNames in glimpse-cache-client.js. The cache /
# slice-group GLIMPSE path instead reads the raw /nodes-export ``name`` field,
# where Workflowy may store a tag wrapped in presentational HTML markup and/or
# HTML entities, e.g. ``#<u>nexus--skills</u>`` (an underline-wrapped pill) or
# entity-encoded ``&amp;`` / ``&lt;`` / ``&gt;``. A bare ``name.split()`` +
# ``startsWith('#')`` then yields the GARBAGE token ``<u>nexus--skills</u>``
# (after the leading '#' strip), which fails the literal-prefix match and
# silently evicts the node from the slice in Phase 1 — taking its ancestors
# with it. (Symptom: cache slice-group GLIMPSE drops members the live F8 modal
# keeps; tag-AGNOSTIC, hits plain members and --expand members alike.)
#
# Fix: strip simple HTML-tag markup and de-whiten the common HTML entities
# from each token BEFORE the '#'-strip, so ``#<u>nexus--skills</u>`` ->
# ``nexus--skills``. The tag pattern mirrors whiten_text_for_header_compare in
# markdown_roundtrip.py (``<[A-Za-z_/][-A-Za-z0-9_/]*>``): it requires an
# alpha/'/' lead so a genuine ``a < 5`` style fragment is untouched, and it
# matches both the opening ``<u>`` and the closing ``</u>``. Kept self-
# contained here (no cross-module import) because this module is deliberately
# transport-free and standalone-testable.
#
# ⚖️ TWIN / NO-DRIFT CONTRACT: this is a deliberate DUPLICATE of
# nexus_map_codebase._strip_workflowy_tag_underline_markup (the canonical,
# older primitive that split_name_and_tags -> list_anchors / update_anchor /
# the add/remove/rename_tag tools all use). We cannot import it here without
# breaking this module's transport-free / standalone design, so the two copies
# are TWINNED on purpose (same pattern as the JS<->Py mirrors elsewhere). To
# guarantee they can never disagree on a node's tags, THIS copy is a strict
# SUPERSET of the canonical one: it strips the same <u>/</u> underline wrapper
# AND the malformed <-u>/<-/u> interim variant (literal + entity-encoded), and
# additionally strips other simple tag markup + de-whitens common entities.
# If you change EITHER function, update BOTH (and keep this one a superset).
# A future proper consolidation (one shared dependency-free string-utils
# primitive) is logged as a TODO; until then, this comment is the guard.
_TAG_MARKUP_RE = re.compile(r"<[A-Za-z_/][-A-Za-z0-9_/]*>")
# Malformed interim close marker (canonical primitive also strips this): the
# emoji-preserving sanitizer once converted '/' to '-' but left the angle
# brackets, producing <-u> / <-/u>. Its leading '-' is NOT matched by
# _TAG_MARKUP_RE's [A-Za-z_/] lead, so it needs its own pattern.
_TAG_MARKUP_MALFORMED_RE = re.compile(r"(?i)<\s*-\s*/?\s*u\s*>")


# @beacon[
#   id=f8policy@strip-tag-markup,
#   slice_labels=slices,
#   kind=ast,
#   comment=Canonicalize a cache-stored tag token by stripping presentational HTML-tag markup (<u></u>, <b>, <code>, ...; pattern mirrors whiten_text_for_header_compare) + de-whitening common HTML entities, BEFORE the leading-'#' strip. This is the DOM-vs-cache PARITY fix: the live F8 path harvests clean data-val tags, but the cache slice-group GLIMPSE path reads the raw /nodes-export name where a tag may arrive as #<u>nexus--skills</u>; without this, the garbage token fails the literal prefix match and Phase 1 silently evicts the (tag-agnostic) member + its ancestors. Feeds _strip_hash + extract_name_tags, so every membership/classify consumer is fixed in one place.,
# ]
def _strip_tag_markup(text: str) -> str:
    """Strip HTML-tag markup + common HTML entities from a tag token.

    Removes simple presentational tags (``<u>``, ``</u>``, ``<b>``,
    ``<code>``, ...) and de-whitens ``&amp;`` / ``&lt;`` / ``&gt;`` /
    ``&quot;`` so a cache-stored token like ``#<u>nexus--skills</u>``
    canonicalizes to ``#nexus--skills``. Leaves all other characters
    (including ``#`` and the tag text itself) intact. Idempotent.
    """
    if not text:
        return text
    # De-whiten FIRST so entity-encoded markup (e.g. &lt;u&gt; / &lt;-u&gt;)
    # collapses to its literal angle-bracket form and is then caught by the
    # tag patterns below -- matching the canonical primitive, which handles
    # both the entity-encoded and the literal forms.
    cleaned = text
    if "&" in cleaned:
        cleaned = (
            cleaned.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
        )
    cleaned = _TAG_MARKUP_RE.sub("", cleaned)
    cleaned = _TAG_MARKUP_MALFORMED_RE.sub("", cleaned)
    return cleaned

# ---------------------------------------------------------------------------
# Default postfix labels. These are the *defaults*; real callers pass the
# live values (from extension_settings) as arguments. They live here so the
# pure functions are usable/testable standalone and so the eventual Options
# UI has a canonical default to ship.
#
# A postfixed variant of base prefix ``P`` is ``P`` + SEP + ``<postfix>``,
# e.g. base ``tag--a`` + deprecated ``deprecated`` -> ``tag--a--deprecated``.
# ---------------------------------------------------------------------------
POSTFIX_SEP = "--"
DEFAULT_AUTO_EXPAND_POSTFIX = "expand"
DEFAULT_SUPPRESS_POSTFIX = "suppress"
DEFAULT_DEPRECATED_POSTFIX = "deprecated"
# LOCAL-ANCHOR postfix (Dan, 2026). Joined to a base anchor tag as
# ``base<SEP><word>`` (e.g. ``wfy-ns--local``) to mark an anchor as LOCAL:
# still a real anchor (it nests and still bounds a ``descendants`` GLIMPSE),
# but HIDDEN from the top-level ``list_anchors`` enumeration. This is a
# SEPARATE postfix vocabulary from the three slice postfixes above -- the
# anchor and slice postfix domains are deliberately not conflated (see
# ``match_anchor_tag``). The Options UI stores a LIST (forward-compat, mirroring
# the slice postfix words); the FIRST configured word is the default applied on
# create, and ALL configured words are recognized on read.
DEFAULT_LOCAL_ANCHOR_POSTFIX = "local"
# FAVORITE-ANCHOR postfix (Dan, 2026 -- Dock favorites). Joined to a base
# anchor tag as ``base<SEP><word>`` (e.g. ``wfy-ns--fav``) to STAR an anchor:
# a favorite is a FULLY-REAL anchor (it nests, bounds a ``descendants``
# GLIMPSE, and is a valid dock.* target) that additionally joins the
# ``dock.list`` favorites shortlist. The Dock tag is a FAMILY: ONE tag per
# attribute (``#wfy-ns--local`` and ``#wfy-ns--fav`` are two separate tags);
# postfixes are NEVER stacked (``--local--fav`` is an unknown postfix, hence
# NOT an anchor tag at all). Same LIST discipline as the local words: the
# FIRST configured word is the default applied on create, ALL are recognized
# on read (``note_session_favorite_postfixes`` in the settings reply).
DEFAULT_FAVORITE_ANCHOR_POSTFIX = "fav"
# DEPRECATED-ANCHOR postfix (Dan, 2026 -- Dock favorites). ``base<SEP><word>``
# (e.g. ``wfy-ns--deprecated``) RETIRES an anchor: the tag is in the Dock
# family and registered, but it NEVER confers Dock identity -- for identity
# purposes it is as if absent (a retired Dock leaves discovery, the glimpse
# root guard, and stops bounding descent; another PROPER family tag on the
# same node wins: ``--deprecated`` + ``--local`` is a LOCAL Dock). There is
# deliberately NO deprecated= API: humans use tag.add / tag.remove /
# tag.rename. ⚠️ DIFFERENT DOMAIN from the slice ``DEFAULT_DEPRECATED_POSTFIX``
# above even though the shipped word is identical -- never alias one to the
# other (``note_session_deprecated_postfixes`` in the settings reply).
DEFAULT_DEPRECATED_ANCHOR_POSTFIX = "deprecated"
# Global never-expand tags: a blanket suppressor list (canonical, '#'-less),
# independent of any slice/topic. No shipped default (empty list).
DEFAULT_GLOBAL_NEVER_EXPAND_TAGS: tuple[str, ...] = ()

# ---------------------------------------------------------------------------
# SERVER-SIDE GLIMPSE completed-node exclusion policy (Dan, 2026).
# ---------------------------------------------------------------------------
# The HARDCODED policy value the server-side (cache-projected) GLIMPSE call
# sites pass as ``exclude_completed=`` into build_slice_subtree_from_cache /
# build_stage1_base_tree. It is a MODULE CONSTANT (not an MCP tool
# argument) ON PURPOSE: the behavior is a fixed policy for now, threaded through
# the pipeline in the architecturally correct place (Phase 1) but deliberately
# NOT exposed to agents via a `glimpse()` flag. Should it ever be promoted to a
# real flag, that flag would gate ONLY server-side (cache) GLIMPSEs -- NEVER a
# client-side (live-DOM) GLIMPSE, which must always mirror the user's own
# expanded GUI state (including their show/hide-completed choice). See the big
# COMPLETED-NODE EXCLUSION POLICY block above build_slice_subtree_from_cache.
SERVER_SIDE_GLIMPSE_EXCLUDE_COMPLETED = True


# ===========================================================================
# MIDDLE-SEGMENT WILDCARD MATCHER  ---  P [ <-middle> ] [ <SEP><postfix> ]
# ===========================================================================
# THE single shared prefix+middle+postfix recognizer (Dan, July 2026). This is
# the ONE place the "a personal middle-segment variant of a slice tag still
# counts as a member, and can be independently deprecated/suppressed/expanded"
# rule lives. Every membership/classification consumer in this module
# (is_slice_member, classify_node_tags, and the deprecated-exclusion) funnels
# through it, so F8, the Markdown modal, and the server-side slice GLIMPSE can
# never diverge on what a tag means.
#
# THE RULE (given a '#'-stripped tag T, a base slice prefix P, and the set of
# recognized postfix words S_words = {expand, suppress, deprecated, ...}):
#
#   1. FRONT:  T must start with P  (literal startsWith -- same front-match
#              Workflowy's own dead-simple tag search does).
#   2. MIDDLE: the remainder R = T[len(P):] (everything after the prefix) is
#              split into an optional MIDDLE and an optional trailing
#              <SEP><postfix>. The MIDDLE must be EMPTY or START WITH '-'
#              (Dan's ruling: a hyphen is the required segment delimiter, so
#              prefix 'comm-jim' matches 'comm-jim', 'comm-jim-teams',
#              'comm-jim--deprecated' but NOT the unrelated family 'comm-jimx'
#              / 'comm-jimmy'). This is stricter than the old naked startsWith,
#              on purpose -- it stops one prefix bleeding into a sibling family.
#   3. POSTFIX: if R ends with <SEP><w> for some recognized postfix word w
#              (the trailing '--<w>' region), that w is the matched postfix and
#              the middle is R with that suffix removed. Otherwise there is no
#              postfix (bare member / plain family variant) and the whole of R
#              is the middle.
#
# Bare P (R empty) and P+S (R == '<SEP><postfix>', empty middle) remain matches
# exactly as before; the NEW capability is P + '-<middle>' [ + <SEP><postfix> ].
#
# ⚠️ DELIBERATE, ACCEPTED DIVERGENCE FROM WORKFLOWY (Dan): Workflowy's live tag
# search is a raw substring match with NO right boundary -- a tag can run on
# into following text with no trailing space. We CANNOT reason about "where the
# postfix is" without a right boundary, so this matcher (and the whole cache
# classifier) assumes the tag ENDS at whitespace -- which is exactly how
# extract_tags_from_text already tokenizes (``#([^\s#]+)``, i.e. up to the next
# whitespace or '#'). In the RARE case where a user writes a tag with NO
# trailing space that runs straight into other text, Workflowy's search may
# surface a match we classify differently (or vice-versa). This is impossible
# to disambiguate without the boundary and is accepted as a rare edge case (the
# same category of sanctioned divergence as the zombie-leaf prune); daily usage
# has shown it is a non-issue. No code works around it by design.


def _recognized_postfix_words(
    *,
    auto_expand_postfix: str = DEFAULT_AUTO_EXPAND_POSTFIX,
    suppress_postfix: str = DEFAULT_SUPPRESS_POSTFIX,
    deprecated_postfix: str = DEFAULT_DEPRECATED_POSTFIX,
    extra_postfixes: Iterable[str] = (),
) -> list[str]:
    """The recognized postfix WORDS as a de-duped set (longest-first).

    Returned longest-first so a postfix word that is a suffix of another can
    never shadow the longer match (mirrors ``normalize_slice_group_tags``).
    ``extra_postfixes`` is forward-compat for the pending "multiple postfix
    words" Options work (WS-1.3 / WG-1.41) -- the recognizer is already a SET
    so that feature only has to widen this list, never the match logic.
    """
    words: list[str] = []
    seen: set[str] = set()
    for w in (
        str(auto_expand_postfix or "").strip(),
        str(suppress_postfix or "").strip(),
        str(deprecated_postfix or "").strip(),
        *[str(x or "").strip() for x in extra_postfixes],
    ):
        if w and w not in seen:
            seen.add(w)
            words.append(w)
    words.sort(key=len, reverse=True)
    return words


# @beacon[
#   id=f8policy@slice-tag-variant-match,
#   slice_labels=slices,agent-kit-vendor,
#   kind=ast,
#   comment=THE single shared middle-segment wildcard matcher: does '#'-stripped tag T match base prefix P as P [ '-<middle>' ] [ '--<postfix>' ]? Front is literal startsWith P; the middle (everything between P and any recognized trailing '--<postfix>') must be EMPTY or start with '-' (hyphen = required segment delimiter, Dan's ruling -- so comm-jim matches comm-jim / comm-jim-teams / comm-jim--deprecated but NOT comm-jimx). Recognized postfix is matched in the trailing '--<word>' region against the LIVE postfix set (POSTFIX_SEP + word). Returns {matched, matched_prefix, middle, postfix} -- postfix is '' for a bare/plain family variant. This is the ONE recognizer is_slice_member + classify_node_tags + the deprecated-exclusion all call, so F8 / Markdown modal / server-side slice GLIMPSE never drift. Accepts the right-boundary-at-whitespace divergence from Workflowy's boundaryless search as a documented, rare, accepted edge case.,
# ]
def slice_tag_variant_match(
    tag_stripped: str,
    prefix: str,
    *,
    postfix_words: Iterable[str],
) -> dict[str, Any] | None:
    """Match one '#'-stripped tag against ONE base prefix with a wildcard middle.

    Returns ``None`` when the tag does not match ``prefix`` under the
    prefix + (empty-or-hyphen) middle + optional recognized-postfix rule; else
    a dict::

        {"matched_prefix": prefix, "middle": <str>, "postfix": <str>}

    where ``middle`` is the arbitrary middle segment (``""`` for the bare
    ``P`` / ``P<SEP><postfix>`` cases, else starting with ``'-'``) and
    ``postfix`` is the recognized postfix word (``""`` when the tag carries no
    recognized trailing ``<SEP><word>``, i.e. a plain family member).
    """
    if not tag_stripped or not prefix:
        return None
    if not tag_stripped.startswith(prefix):
        return None
    remainder = tag_stripped[len(prefix):]

    # Peel a recognized trailing '<SEP><word>' postfix off the remainder, if
    # present. Longest-word-first (via _recognized_postfix_words ordering) so a
    # shorter postfix that is a suffix of a longer one cannot shadow it.
    matched_postfix = ""
    middle = remainder
    for w in postfix_words:
        if not w:
            continue
        suf = POSTFIX_SEP + w
        # The postfix must occupy the trailing '--<word>' region AND leave room
        # for it (len check). We do NOT require a non-empty middle here (P+S is
        # a legal no-middle variant); the middle-boundary rule below vets it.
        if remainder.endswith(suf) and len(remainder) >= len(suf):
            matched_postfix = w
            middle = remainder[: -len(suf)]
            break

    # MIDDLE BOUNDARY RULE: the middle must be EMPTY or START WITH '-' (hyphen
    # is the required segment delimiter). This is what prevents prefix 'comm-jim'
    # from bleeding into the unrelated family 'comm-jimx' / 'comm-jimmy': their
    # middle ('x' / 'my') does not start with '-', so no match.
    if middle and not middle.startswith("-"):
        return None

    return {"matched_prefix": prefix, "middle": middle, "postfix": matched_postfix}


# ===========================================================================
# ANCHOR-TAG MATCHER  ---  base | base<SEP><attribute-word>  (EXACT base only)
# ===========================================================================
# THE single shared anchor-tag classifier (Dan, 2026 -- "local anchors";
# widened to the full Dock-tag FAMILY by the Dock-favorites unit). This
# is the anchor-system analogue of ``slice_tag_variant_match`` above, and it is
# a DELIBERATELY SEPARATE primitive, NOT a reuse of it. The two share only the
# ``POSTFIX_SEP`` vocabulary; their MATCH STRICTNESS differs on purpose:
#
#   * A SLICE tag is a PREFIX/FAMILY (``comm-jim`` matches ``comm-jim-teams``
#     via the wildcard middle) -- so slice matching allows an arbitrary middle.
#   * An ANCHOR tag is a SPECIFIC IDENTITY marker (``wfy-ns``), NOT a family.
#     Anchor matching therefore requires the base to match EXACTLY -- there is
#     NO middle segment -- so ``wfy-ns`` can never bleed into an unrelated
#     ``wfy-nsx`` / ``wfy-ns-project``. This preserves the anchor system's
#     stricter contract (see ``build_anchor_boundary_predicate``'s emphatic
#     "EXACT, not prefix/family" note) rather than cracking it by borrowing the
#     looser slice matcher. Anchor CATEGORIES via dynamic middle segments are a
#     deliberate NON-GOAL (Dan): one flat set of anchor tags IS the
#     categorization; middle-segment anchor types would be over-design.
#
# THE RULE (given a '#'-stripped tag T, the set of configured base anchor tags
# B = {wfy-ns, ...}, and the THREE configured attribute-word lists -- LOCAL
# words L, FAVORITE words F, DEPRECATED words D):
#
#   * T is an ANCHOR-FAMILY tag iff EITHER
#       (a) T == b            for some base b in B   (the BARE base: a Dock
#                             tag asserting NO attribute), OR
#       (b) T == b<SEP>w      for some base b in B and some word w in the
#                             UNION L ∪ F ∪ D -- exactly ONE attribute per
#                             tag (I2: postfixes are never stacked, so
#                             ``b--local--fav`` matches NOTHING).
#   * The matched ATTRIBUTE is ``local`` / ``favorite`` / ``deprecated`` for
#     form (b) (None for the bare base). A word configured in TWO lists
#     (degenerate config) resolves by the FIXED precedence
#     local > favorite > deprecated -- deterministic, no error, no warning.
#   * ``--deprecated`` NEVER confers Dock identity (I4): the matcher still
#     REPORTS it (attribute='deprecated') so consumers can see the family
#     tag, but ``classify_anchor_tags`` treats it as if absent for identity.
#
# There is NO middle segment and NO other postfix vocabulary here: only the
# three registered attribute-word lists are recognized (an anchor tag carrying,
# say, a slice ``--expand`` postfix is simply not an anchor tag by this rule --
# anchors and slice postfixes are separate semantic domains, and we do not
# conflate them; a base that DOUBLES as a slice prefix keeps its ``--expand``
# meaning, §6.1.4 of the favorites spec). Longest-word-first across the UNION
# so a word that is a suffix of another can never shadow the longer match
# (mirrors the slice matcher / the recognizer ordering).


# @beacon[
#   id=f8policy@match-anchor-tag,
#   slice_labels=nexus--tags,nexus--anchors,
#   kind=ast,
#   comment=THE single shared anchor-tag matcher: does '#'-stripped tag T match ONE base anchor tag B under the EXACT-base rule -- T == B (the bare base, attribute None) or T == B<SEP>w for ONE registered attribute word w in the UNION of the local/favorite/deprecated word lists (attribute local|favorite|deprecated; one tag per attribute, postfixes never stacked)? Deliberately EXACT (no middle segment), unlike slice_tag_variant_match, so an anchor tag is a specific identity marker that cannot bleed into a sibling (wfy-ns !-> wfy-nsx / wfy-ns-project). Returns {matched_base, postfix, attribute, is_local} or None. Word collision across two lists resolves deterministically local > favorite > deprecated; longest-word-first across the union so a suffix word never shadows. --deprecated is REPORTED (attribute='deprecated') but never confers Dock identity -- that ruling lives in classify_anchor_tags. Shares only POSTFIX_SEP with the slice matcher; the anchor attribute words are a separate semantic domain from slice --expand/--suppress/--deprecated.,
# ]
def match_anchor_tag(
    tag_stripped: str,
    base_tag: str,
    *,
    local_postfixes: "Iterable[str]",
    favorite_postfixes: "Iterable[str] | None" = None,
    deprecated_postfixes: "Iterable[str] | None" = None,
) -> dict[str, Any] | None:
    """Match one '#'-stripped tag against ONE base anchor tag (EXACT base).

    Returns ``None`` when the tag is neither ``base_tag`` exactly nor
    ``base_tag<SEP><word>`` for a registered attribute word ``word`` in the
    UNION of the three configured word lists; else a dict::

        {"matched_base": base_tag, "postfix": <str>,
         "attribute": None | "local" | "favorite" | "deprecated",
         "is_local": bool}

    where ``postfix`` is the recognized attribute word (``""`` for the bare
    base), ``attribute`` names the ONE attribute the tag carries (``None``
    for the bare base -- it asserts nothing), and ``is_local`` is kept for
    compatibility and equals ``attribute == "local"``.

    ``favorite_postfixes`` / ``deprecated_postfixes`` default (``None``) to
    the shipped default lists ``[DEFAULT_FAVORITE_ANCHOR_POSTFIX]`` /
    ``[DEFAULT_DEPRECATED_ANCHOR_POSTFIX]``; ``local_postfixes`` keeps its
    existing contract (caller-supplied; an empty list recognizes no local
    word). A word configured in TWO lists (degenerate config) resolves by
    the FIXED precedence ``local`` > ``favorite`` > ``deprecated`` --
    deterministic, no error, no warning.

    EXACT base by design: there is NO wildcard middle segment (contrast
    :func:`slice_tag_variant_match`), so ``base='wfy-ns'`` matches ``wfy-ns``
    and ``wfy-ns--local`` but NEVER ``wfy-nsx`` / ``wfy-ns-project``. Stacked
    postfixes (``wfy-ns--local--fav``) and unknown words (``wfy-ns--expand``,
    a typo) match NOTHING: ONE attribute per tag, registered words only.
    """
    b = _strip_hash(base_tag) if base_tag else ""
    t = _strip_hash(tag_stripped) if tag_stripped else ""
    if not b or not t:
        return None
    # Form (a): the bare base -- a Dock tag asserting NO attribute.
    if t == b:
        return {
            "matched_base": b,
            "postfix": "",
            "attribute": None,
            "is_local": False,
        }
    # Form (b): exact base + <SEP><one registered attribute word>. Build the
    # word -> attribute map with the FIXED collision precedence (local >
    # favorite > deprecated) by writing the LOWER-precedence lists first; the
    # last write for a duplicate word wins.
    fav_words = (
        [DEFAULT_FAVORITE_ANCHOR_POSTFIX]
        if favorite_postfixes is None
        else list(favorite_postfixes)
    )
    dep_words = (
        [DEFAULT_DEPRECATED_ANCHOR_POSTFIX]
        if deprecated_postfixes is None
        else list(deprecated_postfixes)
    )
    word_attr: dict[str, str] = {}
    for words, attr in (
        (dep_words, "deprecated"),
        (fav_words, "favorite"),
        (list(local_postfixes or []), "local"),
    ):
        for w in words:
            word = str(w or "").strip()
            if word:
                word_attr[word] = attr
    # Longest-word-first across the UNION so a word that is a suffix of
    # another cannot shadow the longer match.
    for word in sorted(word_attr, key=len, reverse=True):
        if t == f"{b}{POSTFIX_SEP}{word}":
            attribute = word_attr[word]
            return {
                "matched_base": b,
                "postfix": word,
                "attribute": attribute,
                "is_local": attribute == "local",
            }
    return None


# @beacon[
#   id=f8policy@classify-anchor-tags,
#   slice_labels=nexus--tags,nexus--anchors,
#   kind=ast,
#   comment=Classify a node's NAME tags against the configured base anchor tags + the THREE attribute-word lists (local/favorite/deprecated), via the shared match_anchor_tag matcher. UNION semantics (I5): is_anchor True iff at least one PROPER family tag matched (bare base, --local or --favorite); is_local / is_favorite are the union of their attribute over ALL proper family tags across ALL bases (a bare base asserts NOTHING, so #wfy-ns + #wfy-ns--local IS local -- the historical global-form-dominates rule is SUPERSEDED); a --deprecated match NEVER confers Dock identity (I4) and is reported via has_deprecated / family_tags(proper=False) only. Also returns matched_base/matched_tag (the FIRST proper family tag in name order), matched_bases (distinct bases with a proper tag, first-occurrence order -- the dock.update families: detector's input) and family_tags (every family-form tag in name order with base/attribute/postfix/proper). This is the ONE classifier BOTH list_anchors (is_anchor AND NOT is_local) and build_anchor_boundary_predicate (is_anchor -- a favorite or local anchor still stops descent; a deprecated-only node does NOT) call, so anchor-hood + attributes can never drift between the enumeration and the GLIMPSE 'descendants' boundary. Empty base set -> not an anchor. Pure; no I/O.,
# ]
def classify_anchor_tags(
    name_tags: "Iterable[str]",
    base_tags: "Iterable[str]",
    *,
    local_postfixes: "Iterable[str]",
    favorite_postfixes: "Iterable[str] | None" = None,
    deprecated_postfixes: "Iterable[str] | None" = None,
) -> dict[str, Any]:
    """Classify a node's NAME tags for Dock identity + attributes (shared primitive).

    ``name_tags`` are '#'-stripped tags (e.g. from :func:`extract_name_tags`).
    ``base_tags`` are the configured anchor base tags (``note_session_tags``);
    ``local_postfixes`` / ``favorite_postfixes`` / ``deprecated_postfixes``
    are the configured attribute words (the two new lists default, ``None``,
    to the shipped defaults).

    Returns (ALL keys always present)::

        {"is_anchor": bool, "is_local": bool, "is_favorite": bool,
         "attributes": frozenset[str],
         "matched_base": str | None, "matched_tag": str | None,
         "matched_bases": tuple[str, ...],
         "family_tags": tuple[dict, ...],
         "has_deprecated": bool}

    * ``is_anchor`` — True iff at least one PROPER family tag matched
      (attribute ∈ {None, ``'local'``, ``'favorite'``}). A ``--deprecated``
      match NEVER sets this (I4: for identity it is as if absent).
    * ``is_local`` / ``is_favorite`` — UNION semantics (I5): each is True iff
      its attribute is asserted by ANY proper family tag across ALL bases. A
      bare base asserts NOTHING (``#wfy-ns`` + ``#wfy-ns--local`` IS local --
      the superseded historical rule let the global form dominate).
    * ``attributes`` — frozenset, the union over PROPER tags; a bare base
      contributes nothing; ``'deprecated'`` is never a member.
    * ``matched_base`` / ``matched_tag`` — the base and the '#'-stripped tag
      of the FIRST PROPER family tag in ``name_tags`` order (compat: the root
      guard and touch messages use them); None when not a Dock.
    * ``matched_bases`` — distinct bases having at least one PROPER tag, in
      first-occurrence order: the ``dock.update`` ``families:`` detector's
      input. A family present only via ``--deprecated`` is NOT in it.
    * ``family_tags`` — every tag that matched ANY family form, in name
      order: ``{"tag", "base", "attribute", "postfix", "proper"}``; includes
      ``--deprecated`` tags (``proper=False``).
    * ``has_deprecated`` — True iff any ``--deprecated`` family tag is
      present (diagnostic only; no behaviour keys off it).

    THE unification point for anchor recognition. ``list_anchors`` uses
    ``is_anchor and not is_local``; ``build_anchor_boundary_predicate`` uses
    ``is_anchor`` (a local or favorite anchor still bounds a ``descendants``
    GLIMPSE; a deprecated-only node is not a Dock and does not). Both
    call HERE so they cannot diverge. Pure; no I/O.
    """
    bases = [
        _strip_hash(b) for b in (base_tags or []) if isinstance(b, str) and b.strip()
    ]
    bases = [b for b in bases if b]
    if not bases:
        return {
            "is_anchor": False,
            "is_local": False,
            "is_favorite": False,
            "attributes": frozenset(),
            "matched_base": None,
            "matched_tag": None,
            "matched_bases": (),
            "family_tags": (),
            "has_deprecated": False,
        }

    tags = [
        _strip_hash(t) for t in (name_tags or []) if isinstance(t, str) and t.strip()
    ]
    tags = [t for t in tags if t]

    family_tags: list[dict[str, Any]] = []
    attributes: set[str] = set()
    matched_base: str | None = None
    matched_tag: str | None = None
    matched_bases: list[str] = []
    has_deprecated = False
    for t in tags:
        for b in bases:
            m = match_anchor_tag(
                t,
                b,
                local_postfixes=local_postfixes,
                favorite_postfixes=favorite_postfixes,
                deprecated_postfixes=deprecated_postfixes,
            )
            if not m:
                continue
            attribute = m["attribute"]
            proper = attribute != "deprecated"
            family_tags.append({
                "tag": t,
                "base": m["matched_base"],
                "attribute": attribute,
                "postfix": m["postfix"],
                "proper": proper,
            })
            if proper:
                # UNION (I5): every proper family tag asserts its attribute;
                # the bare base asserts nothing but DOES confer identity.
                if attribute is not None:
                    attributes.add(attribute)
                if matched_base is None:
                    matched_base = m["matched_base"]
                    matched_tag = t
                if m["matched_base"] not in matched_bases:
                    matched_bases.append(m["matched_base"])
            else:
                # --deprecated: in the family, recorded, NEVER identity (I4).
                has_deprecated = True
            # EXACT-base rule: a tag matches at most one distinct base string;
            # first configured base wins for this tag.
            break
    return {
        "is_anchor": matched_base is not None,
        "is_local": "local" in attributes,
        "is_favorite": "favorite" in attributes,
        "attributes": frozenset(attributes),
        "matched_base": matched_base,
        "matched_tag": matched_tag,
        "matched_bases": tuple(matched_bases),
        "family_tags": tuple(family_tags),
        "has_deprecated": has_deprecated,
    }


# @beacon[
#   id=f8policy@apply-auto-expand-postfix-per-family,
#   slice_labels=nexus--tags,slices,
#   kind=ast,
#   comment=Per-tag-FAMILY auto-expand postfix rule for add_to_anchor's tags= arg. Groups the union tag set by family (first hyphen-delimited segment); within each family, if NO tag already carries ANY recognized postfix (--expand/--suppress/--deprecated, detected via slice_tag_variant_match) it appends --expand to EVERY tag in that family; if ANY tag in the family already carries a postfix it leaves the WHOLE family untouched (don't-clobber rule). Families are processed INDEPENDENTLY. Pure/deterministic; no I/O. Applied only to a tree ROOT that has descendants (gating lives in the caller).,
# ]
def apply_auto_expand_postfix_per_family(
    tags: "Iterable[str]",
    *,
    auto_expand_postfix: str = DEFAULT_AUTO_EXPAND_POSTFIX,
    postfix_words: "Iterable[str] | None" = None,
) -> list[str]:
    """Return ``tags`` with the auto-expand postfix applied per FAMILY.

    THE rule (Dan, 2026 -- add_to_anchor multi-node capture):
      * Group the '#'-stripped tags by FAMILY = the first hyphen-delimited
        segment (``build-v1`` and ``build-v1-frontend`` are the SAME family
        ``build``; ``comm-jim`` is family ``comm``; a bare ``math`` is family
        ``math``). Families are processed INDEPENDENTLY.
      * Within a family, if NO member already carries ANY recognized postfix
        (``--expand`` / ``--suppress`` / ``--deprecated``, detected via
        :func:`slice_tag_variant_match`), append ``<SEP>expand`` to EVERY
        member of that family. This covers the 'multiple same-family tags,
        none postfixed -> expand them all' ruling AND the single-family case.
      * If ANY member of a family already carries a recognized postfix, LEAVE
        THE WHOLE FAMILY UNTOUCHED (the don't-clobber rule -- pass every tag
        through verbatim, general + specific + already-postfixed alike).

    Input/output are the '#'-prefixed tag strings (leading '#' preserved on
    output; a passed-in tag without '#' is treated by its stripped body but
    re-emitted in the same '#'/no-'#' shape it arrived in). Order is preserved
    (first-seen), and duplicates are collapsed. Pure + deterministic.

    ``postfix_words`` defaults to the recognized set built from the live
    auto-expand/suppress/deprecated postfix words; a caller that has read the
    live Options postfix config passes them so detection matches the user's
    configured words exactly.
    """
    words = list(postfix_words) if postfix_words is not None else _recognized_postfix_words(
        auto_expand_postfix=auto_expand_postfix,
    )
    expand_word = str(auto_expand_postfix or DEFAULT_AUTO_EXPAND_POSTFIX).strip() or DEFAULT_AUTO_EXPAND_POSTFIX

    # Normalize into (had_hash, stripped_body) preserving first-seen order and
    # de-duping on the stripped body.
    items: list[tuple[bool, str]] = []
    seen_bodies: set[str] = set()
    for raw in tags:
        s = str(raw or "").strip()
        if not s:
            continue
        had_hash = s.startswith("#")
        body = s[1:] if had_hash else s
        body = body.strip()
        if not body or body in seen_bodies:
            continue
        seen_bodies.add(body)
        items.append((had_hash, body))

    def _family_of(body: str) -> str:
        # Family = first hyphen-delimited segment. (A recognized postfix is
        # joined by '--', so its leading '-' does not create a spurious family
        # split: the family is still the segment before the FIRST hyphen.)
        return body.split("-", 1)[0]

    def _has_recognized_postfix(body: str) -> bool:
        # A tag 'carries a postfix' iff it matches ITS OWN family prefix with a
        # non-empty recognized postfix. We test against the family stem as the
        # prefix -- the stem always matches (empty middle allowed), so a
        # non-empty ``postfix`` in the result means a real trailing --<word>.
        m = slice_tag_variant_match(body, _family_of(body), postfix_words=words)
        return bool(m and m.get("postfix"))

    # Group by family (preserve first-seen family order).
    family_order: list[str] = []
    by_family: dict[str, list[tuple[bool, str]]] = {}
    for had_hash, body in items:
        fam = _family_of(body)
        if fam not in by_family:
            by_family[fam] = []
            family_order.append(fam)
        by_family[fam].append((had_hash, body))

    out: list[str] = []
    for fam in family_order:
        members = by_family[fam]
        family_has_postfix = any(_has_recognized_postfix(b) for _h, b in members)
        for had_hash, body in members:
            if family_has_postfix:
                new_body = body  # don't-clobber: leave the whole family as-is
            else:
                new_body = body + POSTFIX_SEP + expand_word
            out.append(("#" + new_body) if had_hash else new_body)
    return out


# ===========================================================================
# Tag primitives — MUST mirror hotkeys-shared.js / glimpse-cache-client.js.
# ===========================================================================

def _strip_hash(token: Any) -> str:
    """Return the '#'-stripped, trimmed, markup-free form of a tag token (or '').

    Strips presentational HTML-tag markup + common HTML entities FIRST (so a
    cache-stored ``#<u>nexus--skills</u>`` becomes ``#nexus--skills``), then
    removes a single leading '#'. See ``_strip_tag_markup`` for why this is
    required on the cache path (the live F8 path never sees the markup).
    """
    if not isinstance(token, str):
        return ""
    t = _strip_tag_markup(token.strip()).strip()
    if t.startswith("#"):
        t = t[1:]
    return t


# @beacon[
#   id=f8policy@core-trailing-name-tags,
#   slice_labels=nexus--tags,
#   kind=ast,
#   comment=THE name-only tag reader: return the '#'-stripped tags in a node NAME's TRAILING tag region, via the ONE canonical authority nexus_map_codebase.split_name_and_tags (reached BY IMPORT, never copied -- Pin 1). This is what match_scope='name' means on every semantic tag-search surface (is_slice_member / node_is_curated_for_prefixes / build_tag_predicate / node_is_live_self / build_anchor_boundary_predicate / gather / classify_node_tags / name_has_global_never_tag): a tag is a tag only when it sits at the end of the name, exactly as the add/remove/rename_tag mutation core reads them. Contrast extract_name_tags (whitespace-token, ANYWHERE in the title) -- the pre-flip behaviour, kept only for the free-form search-string parser (parse_search_string_to_base_prefixes); suppression and the postfix classification read THIS reader (084). Lazy function-local import mirrors _resolve_split_name_and_tags (api_client_core): nexus_map_codebase imports nothing from workflowy_mcp, so there is no cycle; the import is cached after first success.
# ]
_core_split_name_and_tags: "Callable[[str], tuple[str, list[str]]] | None" = None


def _core_trailing_name_tags(name: Any) -> list[str]:
    """Return the '#'-stripped tags in ``name``'s TRAILING tag region.

    The name-only scope reader. Routes through the ONE canonical trailing-region
    splitter ``nexus_map_codebase.split_name_and_tags`` -- the same authority the
    add/remove/rename_tag mutation core uses -- so a semantic tag search under
    ``match_scope='name'`` cannot disagree with the mutation half about what a
    tag is. Reached BY IMPORT (Pin 1): never a copied parser. The canonical
    splitter already strips presentational ``<u>`` markup; the returned tags are
    additionally canonicalized through :func:`_strip_hash` (markup + leading
    ``'#'``) so every downstream startswith/prefix test sees the same shape as
    :func:`extract_name_tags` / :func:`extract_node_tags` produce.

    A tag that appears mid-title (prose) or only in the note body is NOT in the
    trailing region and is therefore not returned. A genuine import/split
    failure raises -- there is deliberately NO inline fallback copy: a second
    trailing-region parser is the one failure this bridge exists to prevent.
    """
    if not isinstance(name, str) or not name:
        return []
    global _core_split_name_and_tags
    fn = _core_split_name_and_tags
    if fn is None:
        from nexus_map_codebase import split_name_and_tags as fn  # type: ignore[import-not-found]

        _core_split_name_and_tags = fn
    _base, tags = fn(name)
    out: list[str] = []
    for token in tags:
        stripped = _strip_hash(token)
        if stripped:
            out.append(stripped)
    return out


def extract_name_tags(name: Any) -> list[str]:
    """Return the canonical ('#'-stripped) tags found ANYWHERE in a NAME.

    A whitespace-token harvest over the WHOLE title (mirrors the JS
    tokenization ``name.split(/\\s+/)`` then keep tokens that start with
    '#'): a mid-title ``#tag`` counts. This is the PRE-flip reader -- today
    the only semantic consumer is the free-form search-string parser
    (:func:`parse_search_string_to_base_prefixes`, which interprets a pasted
    Workflowy search); membership, the curation cursor, suppression and the
    postfix classification all read the TRAILING tag region instead (see
    :func:`_core_trailing_name_tags`). Not a membership reader; do not add
    new policy consumers.
    """
    if not isinstance(name, str) or not name:
        return []
    out: list[str] = []
    for part in name.split():
        # Markup may sit before OR after the '#' in the cache name (e.g.
        # ``#<u>tag</u>`` or, defensively, ``<u>#tag</u>``), so test for the
        # '#' on the markup-stripped token rather than the raw split token.
        cleaned = _strip_tag_markup(part).strip()
        if cleaned.startswith("#"):
            stripped = _strip_hash(part)
            if stripped:
                out.append(stripped)
    return out


# A '#'-tag occurrence as Workflowy's SEARCH sees it: a literal '#' followed by
# a run of tag characters, found ANYWHERE in the text (NOT just whitespace-
# delimited, NOT just at the end). Live-confirmed (Dan, July 2026): Workflowy
# surfaces a node for ``#zzztest`` even inside ``word#zzztest`` and inside the
# NOTE body, with no preceding space required -- it is a raw SUBSTRING match.
# The tag-char run is "everything up to whitespace or '#'", which is
# deliberately GENEROUS: downstream matching is always a startswith() prefix /
# postfix test, so trailing characters on the run can never cause a wrong
# prefix match (at worst they ride along harmlessly). We stop at '#' so two
# adjacent tags (``#a#b``) split into two tokens.
_HASH_TAG_RUN_RE = re.compile(r"#([^\s#]+)")


def extract_tags_from_text(text: Any) -> list[str]:
    """Return canonical ('#'-stripped) tags found ANYWHERE in ``text``.

    Workflowy's tag search is a raw SUBSTRING match: ``#foo`` surfaces a node
    whenever the literal ``#foo`` appears anywhere in the node's NAME or NOTE
    -- no preceding whitespace required (``word#foo`` matches), and the NOTE
    body counts in full. This mirrors that: whiten the text (strip ``<u>`` /
    ``<b>`` / ``<code>`` / emphasis markup + de-whiten entities, via
    ``_strip_tag_markup``), then harvest every ``#<run>`` occurrence. The run
    is greedy up to whitespace/``#`` and is left un-trimmed because all
    downstream tests are ``startswith`` (prefix / ``--postfix``), so trailing
    punctuation on a run can never produce a false prefix hit.
    """
    if not isinstance(text, str) or not text:
        return []
    cleaned = _strip_tag_markup(text)
    if "#" not in cleaned:
        return []
    out: list[str] = []
    for m in _HASH_TAG_RUN_RE.finditer(cleaned):
        run = m.group(1).strip()
        if run:
            out.append(run)
    return out


# ---------------------------------------------------------------------------
# STRICT tag recognizer (NOT the generalized Workflowy-search harvester).
# ---------------------------------------------------------------------------
#
# WHY A SECOND, NON-GENERALIZED RECOGNIZER EXISTS. ``extract_tags_from_text``
# above is the GENERALIZED, Workflowy-search-parity harvester: it matches a
# raw ``#<run>`` substring ANYWHERE (no left boundary; greedy up to
# whitespace/'#', trailing punctuation left ON), which is exactly right for
# slice MEMBERSHIP (every consumer does a startswith prefix/postfix test, so
# trailing junk rides along harmlessly and never causes a wrong match). But it
# is TERRIBLE for a HUMAN-FACING TAG LIST: on prose/code-bearing notes it
# harvests garbage like ``').strip()``, ``'-stripped``, ``f8-expand`),`` --
# any ``#`` glued inside code (``.lstrip("#tail")``) or a tag with a trailing
# ``),`` / ``.`` / ``;``.
#
# The recognizer below is the STRICT, UNMOORED counterpart used by Lightning
# Rod's GATHER mode (and any other "show me the REAL tags" surface). It is
# DELIBERATELY NOT Workflowy-parity -- Lightning Rod is already an unmoored,
# divergent-from-Workflowy tool, so it is free to be smarter about what counts
# as a genuine tag. It must NOT be substituted for extract_tags_from_text on
# any membership/classification path.
#
# THE THREE RULES that kill the junk:
#   1. LEFT BOUNDARY: the '#' must sit at start-of-string OR immediately after
#      whitespace or an opening bracket ( ([{ ). A '#' glued to a word / quote
#      / close-paren (``word#x``, ``'#tail``, ``)#y``) is NOT a tag start --
#      this alone eliminates the code-substring false positives.
#   2. BODY CHARSET: after '#', a run of tag characters = anything that is NOT
#      whitespace and NOT one of the tag-TERMINATING punctuation characters
#      (quotes, brackets, and sentence punctuation). This keeps Unicode
#      letters + emoji (the trident, topic emoji, etc.) while refusing '#',
#      quotes, and parens INSIDE the tag.
#   3. RIGHT-TRIM: strip any trailing run of sentence punctuation (.,;:!?) the
#      body may still have absorbed, so ``#f8-expand.`` -> ``f8-expand``.
# A tag must have at least one body character AND at least one letter/digit
# (so a lone ``#-`` or ``#...`` is not a tag).
#
# ``#`` itself is NOT a legal tag body char (Workflowy confirms; also lets
# ``#a#b`` split into two tags), so the body class excludes it.

# Punctuation that ENDS a tag (may appear in text right after a tag but is not
# part of it). Includes quotes, all bracket kinds, and sentence punctuation.
_STRICT_TAG_TERMINATORS = "\"'`()[]{}<>,;:!?"
# Trailing punctuation to trim off the matched body (a subset that commonly
# rides along at a tag's tail in prose; hyphen/underscore/slash are legal tail
# chars of a real tag so they are NOT trimmed).
_STRICT_TAG_TRAILING_TRIM = ".,;:!?"
# A tag: '#' at start-of-string or after whitespace / an opening bracket,
# then a run of non-whitespace, non-'#', non-terminator characters.
_STRICT_TAG_RE = re.compile(
    r"(?:^|(?<=[\s([{]))#([^\s#" + re.escape(_STRICT_TAG_TERMINATORS) + r"]+)"
)
# A tag body must contain at least one "substantive" character -- i.e. NOT be
# made up ENTIRELY of the structural connector punctuation a tag may legally
# contain (hyphen / underscore / dot / slash). This rejects a punctuation-only
# run like #--- or #... WHILE still accepting an emoji-only tag such as the
# trident (an emoji is neither alphanumeric nor connector punctuation, so it
# counts as substantive). An earlier alnum-only rule wrongly rejected
# emoji-only tags, which this system genuinely uses.
_STRICT_TAG_CONNECTOR_ONLY_RE = re.compile(r"^[-_./]+$")
# A tag whose FIRST body character is a DIGIT is rejected (Dan's rule): a
# real semantic tag conventionally starts with a letter (or emoji), so a
# leading-digit run (#134198, #2098, #6840, #1, #14 ...) is almost always a
# stray number harvested from prose/code, not a tag. Cheap, conventional,
# low-false-positive. (Note this does NOT catch letter-leading hex like
# #FFD1D1 -- that is the separate hex rule below.)
_STRICT_TAG_LEADING_DIGIT_RE = re.compile(r"^[0-9]")
# A tag that is EXACTLY a 3- or 6-character pure hex string is rejected as a
# hex COLOR code (#FFD1D1, #92B292, #ccff66, #f00), not a semantic tag. These
# survive markup-stripping at a whitespace boundary and would otherwise show
# up as "tags". The 3/6-hex-char shape is a precise STRUCTURAL match for a
# color code, so the false-positive surface is negligible (a genuine
# slice-label that happens to be exactly 6 hex chars is vanishingly rare; if
# one truly exists it can be renamed). NOTE (v2 gap, deliberately not solved):
# this does NOT help a technical subtree whose CODE/CSS content contains many
# real #RRGGBB literals the user wants gathered vs. excluded -- distinguishing
# "hex I want" from "hex I don't" needs intent (a per-gather exclude/shape
# option), which is a future enhancement. This rule only removes the pure
# color-code shape from the human-facing gather list.
_STRICT_TAG_HEX_COLOR_RE = re.compile(r"^(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def extract_strict_tags_from_text(text: Any) -> list[str]:
    """Return REAL '#'-tags from ``text`` (strict, non-Workflowy-parity).

    The strict, UNMOORED counterpart to :func:`extract_tags_from_text`. Where
    that function faithfully mirrors Workflowy's raw-substring tag search
    (great for membership startswith tests, junky for a human tag list), THIS
    function recognizes only GENUINE tags for display/discovery: a ``#`` at a
    real left boundary (start / after whitespace / after an opening bracket),
    a body of tag characters (Unicode letters, digits, ``-``, ``_``, ``/``,
    emoji -- but NOT ``#``, quotes, brackets, or sentence punctuation), with
    trailing sentence punctuation trimmed and a require-one-substantive-char
    rule. Additionally rejects (Dan) two structural non-tags that survive the
    boundary test: a LEADING-DIGIT run (``#134198`` -- a real tag starts with
    a letter/emoji) and a pure 3/6-char HEX COLOR code (``#FFD1D1`` /
    ``#ccff66`` -- a color literal, not a semantic tag).

    Returns the de-duped-by-caller '#'-stripped runs in match order. NEVER use
    this on a slice membership/classification path -- those require the
    generalized greedy substring semantics of ``extract_tags_from_text``.
    """
    if not isinstance(text, str) or not text:
        return []
    cleaned = _strip_tag_markup(text)
    if "#" not in cleaned:
        return []
    out: list[str] = []
    for m in _STRICT_TAG_RE.finditer(cleaned):
        run = m.group(1).rstrip(_STRICT_TAG_TRAILING_TRIM).strip()
        # Reject, in order:
        #  * EMPTY runs.
        #  * connector-only runs (#--- / #...) -- but NOT emoji-only (trident).
        #  * LEADING-DIGIT runs (#134198, #2098) -- Dan's not-a-tag rule.
        #  * pure 3/6-char HEX COLOR codes (#FFD1D1, #ccff66) -- color, not tag.
        # Everything else (ordinary word tags, emoji-only tags) passes.
        if not run:
            continue
        if _STRICT_TAG_CONNECTOR_ONLY_RE.match(run):
            continue
        if _STRICT_TAG_LEADING_DIGIT_RE.match(run):
            continue
        if _STRICT_TAG_HEX_COLOR_RE.match(run):
            continue
        out.append(run)
    return out


def extract_node_tags(name: Any, note: Any = None) -> list[str]:
    """Union of '#'-tags in a node's NAME + NOTE (Workflowy search semantics).

    Workflowy surfaces a node when a tag matches ANYWHERE in its name OR its
    note. Phase-1 membership and Phase-2 classification therefore must read
    BOTH fields, or the server's projected slice would diverge from what the
    user sees after pasting the compiled search into Workflowy. De-duped,
    order-preserving (name tags first, then note tags).
    """
    tags = extract_tags_from_text(name)
    if note:
        seen = set(tags)
        for t in extract_tags_from_text(note):
            if t not in seen:
                seen.add(t)
                tags.append(t)
    return tags


# @beacon[
#   id=f8policy@normalize-slice-group-tags,
#   slice_labels=nexus--config,slices,
#   kind=ast,
#   comment=Normalize an agent-supplied slice-group tag list (comma-separated string OR list) into canonical '#'-stripped BASE prefixes for storage: strip a leading '#', trim, drop empties/internal-whitespace/embedded-'#', de-dupe (order-preserving), and COLLAPSE any '--<postfix>' variant (auto-expand/suppress/deprecated, the three LIVE postfixes) down to its base so foo--expand/foo--deprecated both become foo. Lenient by design (help the agent out); returns (bases, collapsed_info, dropped) for transparency. Postfix words are passed in (server reads them live from the client) so this never hardcodes policy.,
# ]
def normalize_slice_group_tags(
    raw: Any,
    *,
    auto_expand_postfix: str = DEFAULT_AUTO_EXPAND_POSTFIX,
    suppress_postfix: str = DEFAULT_SUPPRESS_POSTFIX,
    deprecated_postfix: str = DEFAULT_DEPRECATED_POSTFIX,
) -> dict[str, Any]:
    """Coerce an agent-supplied tag list into canonical BASE slice prefixes.

    ``raw`` may be a comma-separated string (``"#foo, bar--expand , baz"``) OR
    a list of strings. Each token is canonicalized like the slice-group
    prefix editor: a single leading ``#`` is stripped, surrounding whitespace
    trimmed, and a token that is empty / contains internal whitespace / still
    contains a ``#`` is DROPPED.

    THE BUSINESS RULE (lenient "help the agent out"): a token that carries a
    POSTFIX VARIANT — i.e. ends with ``<SEP><postfix>`` where ``<postfix>`` is
    one of the three LIVE postfixes (auto-expand / suppress / deprecated) and
    ``<SEP>`` is exactly ``POSTFIX_SEP`` (``--``) — is COLLAPSED to its base
    (everything before that suffix). So ``foo--expand``, ``foo--suppress``,
    ``foo--deprecated`` all collapse to the single base ``foo``. Malformed
    near-misses are handled precisely: a trailing ``--`` with nothing after it,
    or a base that would be empty after collapse (e.g. ``--expand`` → empty
    base), is treated as malformed and the token is DROPPED rather than
    silently producing an empty/garbage base. Only an EXACT ``<SEP><postfix>``
    tail is collapsed; ``foo----expand`` (4 hyphens → base ``foo--``) or
    ``fooexpand`` (no sep) are NOT postfix variants by this rule — the former
    would leave a base ending in ``--`` which we reject as malformed, the
    latter is just an ordinary base tag.

    Postfix words are passed in (the server reads them LIVE from the client so
    the single-source-of-truth contract holds); callers that don't have them
    fall back to the shipped defaults.

    Returns a dict:
      * ``bases``     — the de-duped (order-preserving) list of canonical,
                        ``#``-stripped base prefixes to STORE on the group.
      * ``collapsed`` — list of ``{input, base, postfix}`` for every token that
                        was collapsed from a postfix variant (so the caller can
                        report "I stripped these variants" to the agent).
      * ``dropped``   — list of raw tokens dropped as unusable/malformed.
    Pure + deterministic; no I/O.
    """
    # Accept a comma-separated string OR an already-split list.
    if isinstance(raw, str):
        tokens = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        tokens = list(raw)
    else:
        tokens = []

    # The three live postfix suffixes (e.g. '--expand'), longest-first so a
    # postfix that is a suffix of another can't shadow the longer match.
    postfix_words = [
        str(auto_expand_postfix or "").strip(),
        str(suppress_postfix or "").strip(),
        str(deprecated_postfix or "").strip(),
    ]
    suffixes = sorted(
        (POSTFIX_SEP + w for w in postfix_words if w),
        key=len,
        reverse=True,
    )

    bases: list[str] = []
    seen: set[str] = set()
    collapsed: list[dict[str, str]] = []
    dropped: list[str] = []

    for tok in tokens:
        if not isinstance(tok, str):
            dropped.append(repr(tok))
            continue
        original = tok
        s = _strip_hash(tok)  # strip one leading '#', trim
        if not s:
            # empty / whitespace-only token: silently skip (not "dropped" noise)
            if original.strip():
                dropped.append(original)
            continue
        # Reject tokens with internal whitespace or an embedded '#' (mirrors
        # sanitizeExpandTagPrefix): they are not valid single tags.
        if any(ch.isspace() for ch in s) or "#" in s:
            dropped.append(original)
            continue
        # COLLAPSE an exact '<SEP><postfix>' tail to the base. We match when
        # the token ENDS WITH the suffix (len(s) >= len(suf)); a token that IS
        # exactly the suffix (empty base, e.g. '--expand') still counts as a
        # postfix variant here so it is treated as MALFORMED and dropped
        # below, never kept as a bogus base.
        matched_postfix = ""
        s_collapsed = s
        for suf in suffixes:
            if s.endswith(suf):
                s_collapsed = s[: -len(suf)]
                matched_postfix = suf[len(POSTFIX_SEP):]
                break
        if matched_postfix:
            # Malformed if the collapse leaves an empty base (the token was
            # exactly '<SEP><postfix>') or a base still ending in the separator
            # (e.g. 'foo----expand' -> 'foo--').
            if not s_collapsed or s_collapsed.endswith(POSTFIX_SEP):
                dropped.append(original)
                continue
            collapsed.append({"input": _strip_hash(original) or original,
                              "base": s_collapsed, "postfix": matched_postfix})
            s = s_collapsed
        # De-dupe, order-preserving.
        if s not in seen:
            seen.add(s)
            bases.append(s)

    return {"bases": bases, "collapsed": collapsed, "dropped": dropped}


# @beacon[
#   id=f8policy@parse-slice-policy-config,
#   slice_labels=nexus--config,slices,
#   kind=ast,
#   comment=The ONE get_slice_group_config REPLY parser for the policy operands (post-IMPPTX audit, Finding 5): given the live Direction-A reply, return {auto_expand, suppress, deprecated, global_never_expand} with DEFAULT_* fallbacks and the global-never list VALIDATED (must be a list of non-empty strings, else [] -- a malformed string is NEVER character-split into bogus single-letter stems). Consumed by EVERY Stage-0 originator that reads the live postfix config so the reply means ONE thing everywhere: _resolve_slice_group_config (the MCP-return helper), the _handle_render_slice_modal slice cell + Lightning render_tags 'slice' branch + render_symbols suppression read, _project_slice_base_tree_for_scoped_lightning, and the F8 expansion-plan handler. Transport stays per-originator (each keeps its own WS read + ghost-endpoint handling); the INTERPRETATION of the reply is unified here. Pure; no I/O.,
# ]
def parse_slice_policy_config(reply: dict[str, Any]) -> dict[str, Any]:
    """Parse a ``get_slice_group_config`` reply into normalized policy operands.

    Returns ``{auto_expand, suppress, deprecated, global_never_expand}`` with
    the ``DEFAULT_*`` fallbacks. ``global_never_expand`` must be a list of
    non-empty strings; anything else (missing, wrong type, blank entries)
    degrades to ``[]`` -- a malformed string is NEVER character-split into
    bogus single-letter global-never stems.
    """
    reply = reply if isinstance(reply, dict) else {}
    gne_raw = reply.get("f8_global_never_expand_tags")
    if not isinstance(gne_raw, list):
        gne: list[str] = []
    else:
        gne = [str(t) for t in gne_raw if str(t).strip()]
    return {
        "auto_expand": str(reply.get("f8_postfix_auto_expand") or DEFAULT_AUTO_EXPAND_POSTFIX),
        "suppress": str(reply.get("f8_postfix_suppress") or DEFAULT_SUPPRESS_POSTFIX),
        "deprecated": str(reply.get("f8_postfix_deprecated") or DEFAULT_DEPRECATED_POSTFIX),
        "global_never_expand": gne,
    }


# Grammar-regex constants for parse_slice_group_name (module-level so the
# compiled patterns are shared by every call).
_SLICE_GROUP_LEADING_INDEX_RE = re.compile(r"^\s*\[(-?\d+)\]\s*")
_SLICE_GROUP_TRAILING_INDEX_RE = re.compile(r"\[(-?\d+)\]\s*$")


# @beacon[
#   id=f8policy@parse-slice-group-name,
#   slice_labels=nexus--config,slices,
#   kind=ast,
#   comment=THE server-side parser for the slice-group NAME grammar (pipe-delimited category path + optional trailing [n] order index on the last segment; a LEADING [n] marks a category/expander row). Bug-for-bug port of the client's parseSliceGroupName in glimpse-cache-client.js (the display authority) so server-side handle resolution and the agent-facing public view interpret a stored name EXACTLY the way the widget displays it: same expander quirk (the expander branch does NOT strip a trailing [n] from its last segment), same empty-segment survival ('nexus|' -> ['nexus','']), same literal treatment of an unparseable bracket ('[foo]nexus' is just a name). Derives short_name (the final segment; for a LEAF its trailing [n] index is stripped) -- the semantic name accepted as a group_handle form and shown in compact displays. Pure; no I/O. If the grammar ever changes, BOTH runtimes must change together (locked by tests/test_parse_slice_group_name.py).,
# ]
def parse_slice_group_name(name: Any) -> dict[str, Any]:
    """Parse a stored slice-group NAME into its display-grammar parts.

    This is the server-side port of the client ``parseSliceGroupName``
    (glimpse-cache-client.js) -- the ONE grammar, implemented once per
    runtime, kept deliberately bug-for-bug identical:

      * ``A|B|Leaf``     -> path segments ``['A', 'B', 'Leaf']`` (pipe is the
                            nesting delimiter; each segment is trimmed).
      * ``Leaf [n]``     -> a trailing bracketed integer ON THE LAST segment
                            is the row's order index and is stripped from
                            that segment (negative integers allowed).
      * ``[n]A|B``       -> a LEADING bracketed integer marks a category /
                            expander row; ``n`` is the terminal segment's
                            order among its siblings. The expander branch
                            does NOT also strip a trailing ``[n]`` (the JS
                            behavior, mirrored verbatim).
      * ``[n]`` alone    -> degenerate: NOT an expander; the whole trimmed
                            string is a single ordinary segment.
      * ``[foo]nexus``   -> an unparseable bracket is literal text, not an
                            index.

    Returns ``{is_expander, segments, index, short_name}`` where
    ``short_name`` is the final segment (for a leaf, index-stripped) -- the
    semantic short name. Pure + deterministic; no I/O.
    """
    raw = name if isinstance(name, str) else ""
    lead = _SLICE_GROUP_LEADING_INDEX_RE.match(raw)
    if lead:
        rest = raw[lead.end():]
        segments = [seg.strip() for seg in rest.split("|")]
        if not segments or (len(segments) == 1 and segments[0] == ""):
            # '[2]' with no path -> not a usable expander; keep it a plain group.
            only = raw.strip()
            return {
                "is_expander": False,
                "segments": [only],
                "index": None,
                "short_name": only,
            }
        return {
            "is_expander": True,
            "segments": segments,
            "index": int(lead.group(1)),
            "short_name": segments[-1],
        }
    parts = [seg.strip() for seg in raw.split("|")]
    index = None
    last = parts[-1] if parts else ""
    tail = _SLICE_GROUP_TRAILING_INDEX_RE.search(last)
    if tail:
        index = int(tail.group(1))
        parts[-1] = last[: tail.start()].strip()
    return {
        "is_expander": False,
        "segments": parts,
        "index": index,
        "short_name": parts[-1] if parts else "",
    }


# @beacon[
#   id=f8policy@build-slice-group-display-items,
#   slice_labels=nexus--config,nexus--mcp-api,slices,
#   kind=ast,
#   comment=THE server-side Python twin of the client's buildNestedDisplayItems(..., expandAll=true) nested display builder (glimpse-cache-client.js) -- the exact tree/flatten policy behind the compact slice_group.list inventory. Faithful to the GUI grammar, not a resort-by-path: (1) every stored name is parsed through the ONE parse_slice_group_name grammar; (2) missing intermediate category paths are AUTO-VIVIFIED as synthetic category nodes; (3) a leaf attaches to the category node of ALL BUT its final segment; (4) a leading-[n] record is an explicit expander definition for its category path; (5) when several expander definitions target one path, the lowest-indexed definition is canonical and owns the descendants; the rest render as empty duplicate rows; (6) leaves and categories intermix per level, sorted by (order index, nulls last; case-insensitive display name) -- negative indices and gaps are valid; (7) every category is expanded (the server keeps no persisted collapse state). Returns a flat list of {kind:'leaf'|'expander', name, depth, group, duplicate, synthetic} rows in display order. Pure; no I/O. If the GUI builder ever changes, BOTH runtimes must change together.,
# ]
def build_slice_group_display_items(groups: Iterable[dict]) -> list[dict[str, Any]]:
    """Flatten stored slice groups into the GUI's nested display order.

    The exact Python twin of the client's ``buildNestedDisplayItems(items,
    parseSliceGroupName, expandedPaths, /* expandAll */ true)``
    (glimpse-cache-client.js) -- deliberately bug-for-bug, not a re-read of
    the name grammar:

      * every record's stored ``name`` is parsed through the ONE
        :func:`parse_slice_group_name`;
      * intermediate category paths referenced only by descendants are
        AUTO-VIVIFIED as synthetic category nodes;
      * a leaf group attaches to the category node of ALL BUT its final
        segment (the final segment is its display name);
      * a leading-``[n]`` record is an explicit expander definition for its
        category path;
      * when multiple expander definitions target one category path, the
        lowest-indexed definition is CANONICAL and owns the descendants
        (a definition with all-null indices resolves canonical by insertion
        order); every other definition renders as an empty duplicate row;
      * leaves and categories intermix at each level, sorted by
        ``(index with null last, case-insensitive display name)`` --
        negative indices and gaps are valid;
      * every category is emitted expanded (the server keeps no persisted
        collapse state, so ``expandAll`` is the only mode).

    Returns the flat display-order row list: ``{kind: 'leaf'|'expander',
    name, depth, group, duplicate, synthetic}`` (``group`` is the source
    record for leaf rows and explicit-expander rows, None for synthetic
    categories). Pure; no I/O.
    """

    def _make_node(name: str, path: list[str]) -> dict[str, Any]:
        return {
            "name": name,
            "path": path,
            "path_key": "|".join(path),
            "expander_defs": [],
            "leaves": [],
            "children": {},
        }

    root = _make_node("", [])

    def _walk_create(segments: list[str]) -> dict[str, Any]:
        node = root
        for seg in segments:
            if seg not in node["children"]:
                node["children"][seg] = _make_node(seg, node["path"] + [seg])
            node = node["children"][seg]
        return node

    for group in groups or []:
        if not isinstance(group, dict):
            continue
        parsed = parse_slice_group_name(group.get("name"))
        segments = list(parsed["segments"])
        if parsed["is_expander"]:
            _walk_create(segments)["expander_defs"].append(
                {"group": group, "index": parsed["index"]}
            )
        elif len(segments) <= 1:
            root["leaves"].append(
                {
                    "group": group,
                    "index": parsed["index"],
                    "name": (segments[0] if segments else "")
                    or str(group.get("name") or ""),
                }
            )
        else:
            parent = _walk_create(segments[:-1])
            parent["leaves"].append(
                {"group": group, "index": parsed["index"], "name": segments[-1]}
            )

    out: list[dict[str, Any]] = []

    def _emit(node: dict[str, Any], depth: int) -> None:
        entries: list[dict[str, Any]] = []
        for leaf in node["leaves"]:
            entries.append(
                {
                    "kind": "leaf",
                    "index": leaf["index"],
                    "name": leaf["name"],
                    "group": leaf["group"],
                }
            )
        for child in node["children"].values():
            if child["expander_defs"]:
                indices = [
                    d["index"]
                    for d in child["expander_defs"]
                    if d["index"] is not None
                ]
                min_index = min(indices) if indices else None
                canonical_assigned = False
                for definition in child["expander_defs"]:
                    is_canonical = (not canonical_assigned) and (
                        definition["index"] == min_index
                    )
                    if is_canonical:
                        canonical_assigned = True
                    entries.append(
                        {
                            "kind": "expander",
                            "index": definition["index"],
                            "name": child["name"],
                            "group": definition["group"],
                            "child": child if is_canonical else None,
                            "duplicate": not is_canonical,
                            "synthetic": False,
                        }
                    )
            else:
                entries.append(
                    {
                        "kind": "expander",
                        "index": None,
                        "name": child["name"],
                        "group": None,
                        "child": child,
                        "duplicate": False,
                        "synthetic": True,
                    }
                )
        entries.sort(
            key=lambda entry: (
                float("inf") if entry["index"] is None else entry["index"],
                str(entry["name"] or "").lower(),
            )
        )
        for entry in entries:
            if entry["kind"] == "leaf":
                out.append(
                    {
                        "kind": "leaf",
                        "group": entry["group"],
                        "name": entry["name"],
                        "depth": depth,
                        "duplicate": False,
                        "synthetic": False,
                    }
                )
            else:
                out.append(
                    {
                        "kind": "expander",
                        "group": entry["group"],
                        "name": entry["name"],
                        "depth": depth,
                        "duplicate": entry["duplicate"],
                        "synthetic": entry["synthetic"],
                    }
                )
                if entry["child"] is not None:
                    _emit(entry["child"], depth + 1)

    _emit(root, 0)
    return out


# @beacon[
#   id=f8policy@parse-search-string-to-base-prefixes,
#   slice_labels=slices,
#   kind=ast,
#   comment=Parse a FREE-FORM Workflowy search string (manual-F8 override) into canonical '#'-stripped BASE slice prefixes. Harvests only '#'-tag tokens via extract_name_tags (so a leading '-' negation, the '>' subtree-order operator, parentheses, the 'OR' keyword, and any raw prose are ignored -- a '-#foo' token starts with '-' not '#', so it is naturally dropped), then runs them through normalize_slice_group_tags to strip any trailing '--<postfix>' (auto-expand/suppress/deprecated, read live from options) down to the base and de-dupe. Deliberately simple: covers both a hand-pasted plain '#a OR #b' search AND an advanced curated search; anything that is not a legal '#'-tag is discarded. Postfix words are passed in (single source of truth).,
# ]
def parse_search_string_to_base_prefixes(
    search_string: Any,
    *,
    auto_expand_postfix: str = DEFAULT_AUTO_EXPAND_POSTFIX,
    suppress_postfix: str = DEFAULT_SUPPRESS_POSTFIX,
    deprecated_postfix: str = DEFAULT_DEPRECATED_POSTFIX,
) -> list[str]:
    """Parse a free-form search string into canonical base slice prefixes.

    The manual-F8 override path: the user pastes (or hand-curates) a Workflowy
    search string and presses F8. Unlike the Apply/Copy buttons -- which carry
    an explicit slice group -- a manual F8 has only the search box to go on, so
    we derive the slice's BASE prefixes straight from it.

    Algorithm (intentionally simple; see the beacon comment):
      1. Harvest every ``#``-tag token via ``extract_name_tags`` (whitespace
         split, keep ``#``-prefixed tokens). This drops, for free, the
         negation ``-`` (a ``-#foo`` token starts with ``-``), the ``>``
         subtree-order operator, parentheses, the ``OR`` keyword, and any raw
         prose -- none of those are ``#``-tokens.
      2. Feed the harvested tags through ``normalize_slice_group_tags`` (a
         list, not a comma string) to COLLAPSE any trailing ``--<postfix>``
         (auto-expand / suppress / deprecated) to its base and DE-DUPE.

    Returns the de-duped, order-preserving list of ``#``-stripped base
    prefixes. Returns ``[]`` for a missing/blank/garbage search string -- the
    caller then runs Phase 2 with NO active prefixes (global-never-expand tags
    still apply, since those are exact-tag matches independent of prefixes).

    Pure + deterministic; no I/O.
    """
    if not isinstance(search_string, str) or not search_string.strip():
        return []
    harvested = extract_name_tags(search_string)  # '#'-stripped tag bases
    if not harvested:
        return []
    result = normalize_slice_group_tags(
        harvested,  # pass the list form (NOT a comma string)
        auto_expand_postfix=auto_expand_postfix,
        suppress_postfix=suppress_postfix,
        deprecated_postfix=deprecated_postfix,
    )
    return list(result.get("bases") or [])


# @beacon[
#   id=f8policy@tag-matches-prefix,
#   slice_labels=slices,
#   kind=ast,
#   comment=Literal startsWith prefix test on a '#'-stripped tag against a list of '#'-stripped active prefixes — byte-for-byte mirror of JS tagTokenMatchesExpandPrefix so server and client never drift.,
# ]
def tag_matches_prefix(tag_stripped: str, active_prefixes: Iterable[str]) -> bool:
    """True iff ``tag_stripped`` begins with any active prefix (literal startsWith).

    ⚠️ LOW-LEVEL PRIMITIVE ONLY -- NOT the slice-family matcher. This is the
    naked ``startsWith`` mirror of the JS ``tagTokenMatchesExpandPrefix`` used
    by the client's F8 "which visible roots carry an expand-prefix tag"
    detection (where ``wfy-`` must still match ``wfy-ns`` etc.). It is NOT
    hyphen-boundary aware, so it MUST NOT be used for slice membership /
    classification / curation: those go through :func:`slice_tag_variant_match`
    (the ONE shared middle-segment matcher, which enforces the empty-or-hyphen
    middle boundary so ``comm-jim`` does not bleed into ``comm-jimx``). Kept
    only as this documented primitive + a JS-parity anchor.
    """
    if not tag_stripped:
        return False
    for prefix in active_prefixes:
        if prefix and tag_stripped.startswith(prefix):
            return True
    return False


def _deprecated_variants(active_prefixes: Iterable[str], deprecated_postfix: str) -> list[str]:
    """The set of deprecated-variant *prefixes* for the active prefixes.

    A node is deprecated-for-an-active-prefix iff it carries a tag beginning
    with ``<active_prefix>`` AND (somewhere) a tag beginning with
    ``<active_prefix><SEP><deprecated_postfix>``. We represent the deprecated
    variant as its own prefix so a *further*-postfixed deprecated tag still
    matches (literal startsWith, consistent with primitive #2).
    """
    suffix = POSTFIX_SEP + deprecated_postfix
    return [p + suffix for p in active_prefixes if p]


# @beacon[
#   id=f8policy@is-slice-member,
#   slice_labels=slices,
#   kind=ast,
#   comment=THE single membership predicate (per-prefix OR). A node is in the slice iff SOME active prefix P matches one of its tags AND no tag matches P--deprecated. Per-prefix scoping mirrors Workflowy's negation-binds-to-preceding-tag semantics (#P -#P--deprecated OR ...), so deprecating ONE tag does not evict a node still live via another. match_scope (SELECTION-stage scope knob) picks WHERE tags are read: 'name_and_note' (default) -> extract_node_tags (Workflowy-search parity, note counts); 'name' -> _core_trailing_name_tags (the NAME's TRAILING tag region via the core splitter nexus_map_codebase.split_name_and_tags -- a mid-title prose '#tag' or a note-only '#tag' does NOT count; the accepted divergence from a pasted Workflowy search, which has no title-only mode). match_scope is ORTHOGONAL to the --deprecated OPERATION semantics (unchanged). Shared by Phase 1 membership, the compiled search string, and Phase 2's live-hit rescue so the three can never drift.,
# ]
def is_slice_member(
    name: Any,
    active_prefixes: Iterable[str],
    *,
    note: Any = None,
    deprecated_postfix: str = DEFAULT_DEPRECATED_POSTFIX,
    match_scope: str = "name_and_note",
) -> bool:
    """Per-prefix membership: ∃ active prefix P with (matches P) ∧ ¬(matches P--deprecated).

    This is THE membership rule. It is per-PREFIX, not per-node: a single
    non-deprecated active prefix grants membership, even if OTHER active
    prefixes on the same node are deprecated. A node falls out of the slice
    only when EVERY active prefix it carries is deprecated. This exactly
    mirrors Workflowy's compiled search ``#P -#P--deprecated OR ...`` where
    each negation binds only to its immediately-preceding tag.

    ``match_scope`` is the SELECTION-stage scope knob (keyword-only), the ONE
    place membership decides WHERE to read the node's tags:
      * ``"name_and_note"`` (default -- prior behavior) reads tags from the
        node's NAME *and* NOTE (``extract_node_tags``), because Workflowy's tag
        search matches a tag ANYWHERE in either field -- so a node carrying the
        tag only in its note is still a member, exactly as the pasted search
        would surface it.
      * ``"name"`` reads tags from the NAME (title) ONLY, and -- because this
        is a SEMANTIC tag search -- only from the name's TRAILING tag region,
        via the core splitter (``_core_trailing_name_tags`` ->
        ``nexus_map_codebase.split_name_and_tags``). A tag mentioned mid-title
        as prose, or living only in the note body, does NOT grant membership.
        (This is the one place the server render deliberately diverges from a
        pasted-into-Workflowy search, which has no title-only mode -- an
        accepted, documented divergence.)
    Any unrecognized value falls safe to the broader name+note harvest. This is
    purely SELECTION; it never touches the ``--deprecated`` OPERATION semantics
    below, which stay exactly as they were.
    """
    if match_scope == "name":
        tags = _core_trailing_name_tags(name)
    else:
        tags = extract_node_tags(name, note)
    if not tags:
        return False
    # MIDDLE-SEGMENT WILDCARD (Dan, July 2026): a tag matches prefix P when it is
    # P [ '-<middle>' ] [ '--<postfix>' ] (see slice_tag_variant_match). So the
    # per-prefix membership question "does prefix P match, and is P NOT
    # deprecated on this node?" is answered PER-PREFIX over the wildcard matcher:
    #   * P matches iff SOME tag matches P (any middle, any/no postfix).
    #   * P is deprecated iff SOME tag matches P with postfix == deprecated_postfix
    #     (INCLUDING a middle-segment variant, e.g. comm-jim-teams--deprecated
    #     counts as deprecated on prefix comm-jim -- the bug this fixes).
    # A prefix is LIVE iff it matches and is not deprecated; one live prefix
    # keeps the node in (per-prefix OR, unchanged). Recognizing ONLY the
    # deprecated postfix here (not suppress/expand) preserves the exact
    # membership semantics: only --deprecated evicts; --suppress/--expand are
    # presentation, not membership.
    postfix_words = _recognized_postfix_words(deprecated_postfix=deprecated_postfix)
    for prefix in active_prefixes:
        if not prefix:
            continue
        matched_prefix = False
        deprecated_on_prefix = False
        for t in tags:
            m = slice_tag_variant_match(t, prefix, postfix_words=postfix_words)
            if m is None:
                continue
            matched_prefix = True
            if m["postfix"] == deprecated_postfix:
                deprecated_on_prefix = True
        if matched_prefix and not deprecated_on_prefix:
            return True  # this prefix is live -> node is a member
    return False


# @beacon[
#   id=f8policy@node-is-curated-for-prefixes,
#   slice_labels=slices,
#   kind=ast,
#   comment=Form-B (GLIMPSE since='curated') CURATION predicate. True iff the node carries ANY tag that matches an active slice prefix under the SHARED middle-segment matcher (slice_tag_variant_match), read under the SAME match_scope SELECTION-stage scope knob is_slice_member uses ('name' -> _core_trailing_name_tags (the NAME's TRAILING tag region via the core splitter), so neither a mid-title prose family tag nor a family tag living only in a note body curates and neither can move the cursor; 'name_and_note' default -> extract_node_tags(NAME or NOTE, Workflowy-search parity) -- the scope plumbing the server render callers thread into the since='curated' pivot, fixing the Session-019 misread where prose quoting a literal #curated in a note (ap:SP9EY1) advanced a match_scope='name' cursor past every earlier uncurated capture) -- INCLUDING the --deprecated postfix AND middle-segment variants (comm-jim-teams--deprecated curates the comm-jim slice), because a --deprecated tag is ITSELF a curation act (the user tagged it, then triaged it; they must NOT re-see it in the uncurated tail). EXCLUDING the --suppress / --expand postfix variants: those are PRESENTATION (a lens carve-out / auto-expansion -- e.g. a nested program Dock root tagged #curated--suppress to keep its subtree out of a tail), NOT a triage decision; counting them let a suppressed Dock root become the newest 'curated' node and advance the high-water mark past everything captured before it (Session-019 curated-cursor bug). Still DELIBERATELY looser than is_slice_member on the DEPRECATED axis (a node deprecated on every prefix is NOT a member but IS curated), but IDENTICAL on the FAMILY-BOUNDARY axis: it uses the same hyphen-delimited matcher, so comm-jimx does NOT curate the comm-jim slice (a different family). Using the shared matcher (not the loose tag_matches_prefix) keeps the family boundary consistent with membership/classification everywhere.,
# ]
def node_is_curated_for_prefixes(
    name: Any,
    active_prefixes: Iterable[str],
    *,
    note: Any = None,
    auto_expand_postfix: str = DEFAULT_AUTO_EXPAND_POSTFIX,
    suppress_postfix: str = DEFAULT_SUPPRESS_POSTFIX,
    deprecated_postfix: str = DEFAULT_DEPRECATED_POSTFIX,
    match_scope: str = "name_and_note",
) -> bool:
    """True iff the node carries any tag matching an active slice prefix.

    The Form-B (``since="curated"``) curation predicate. Reads tags under the
    ``match_scope`` SELECTION-stage scope knob (third bullet below) and tests
    each against the active prefixes with the SHARED middle-segment matcher
    :func:`slice_tag_variant_match` (NOT the loose ``tag_matches_prefix``).
    Two properties, deliberately different on two different axes -- plus one
    property deliberately IDENTICAL to membership:

    * POSTFIX axis -- TRIAGE counts, PRESENTATION does not. A
      ``--deprecated`` variant (bare OR middle-segment) still counts as
      curated, because tagging-then-triaging a node is itself a curation act
      the user must not re-encounter in the uncurated tail; so a node
      deprecated on every prefix is NOT a slice member but IS curated.
      ``--suppress`` / ``--expand`` variants (bare OR middle-segment) do NOT
      count: they are presentation -- a lens carve-out / auto-expansion, e.g.
      a nested program Dock root tagged ``#curated--suppress`` so its subtree
      stays out of a tail -- not a triage decision. Counting them let such a
      node become the newest "curated" one and advance the curation
      high-water mark past everything captured before it (the Session-019
      curated-cursor bug). A node whose ONLY matching tags carry presentation
      postfixes is therefore NOT curated; one that also carries a plain or
      ``--deprecated`` variant of the family IS.
    * FAMILY-BOUNDARY axis -- STRICT (same as membership): the matcher's
      hyphen-delimited middle rule applies, so ``comm-jimx`` does NOT curate
      the ``comm-jim`` slice (it is a different family). This is the fix that
      keeps the family boundary consistent with is_slice_member /
      classify_node_tags rather than drifting back to naked ``startswith``.
    * SCOPE axis -- SAME as membership: ``match_scope`` (keyword-only) is the
      SELECTION-stage scope knob, the ONE place this predicate decides WHERE
      to read the node's tags, and it makes exactly the same extraction
      choice :func:`is_slice_member` does. ``"name_and_note"`` (default --
      prior behavior) reads tags from the NAME *and* NOTE via
      :func:`extract_node_tags` (Workflowy-search parity: a tag ANYWHERE in
      either field counts); ``"name"`` reads the NAME's TRAILING tag region
      ONLY via the core splitter (``_core_trailing_name_tags`` ->
      ``nexus_map_codebase.split_name_and_tags``), so neither a mid-title prose
      family tag NOR a family tag living only in a note body curates the node,
      and neither can move the curation high-water mark (the Session-019
      misread this plumbs: prose quoting a literal ``#curated`` -- e.g.
      [ap:SP9EY1] -- advanced a ``match_scope='name'`` cursor past every
      earlier uncurated capture). Any unrecognized value falls safe to the
      broader name+note harvest. Purely SELECTION -- orthogonal to the
      postfix/triage semantics above, exactly as in :func:`is_slice_member`.
    """
    # match_scope (SELECTION-stage scope knob) picks WHERE tags are read --
    # the SAME extraction choice is_slice_member makes, so the curation pivot
    # and slice membership can never diverge on scope: 'name' -> title only;
    # anything else (default 'name_and_note', incl. unrecognized values)
    # falls safe to the broader Workflowy-search-parity harvest.
    if match_scope == "name":
        tags = _core_trailing_name_tags(name)
    else:
        tags = extract_node_tags(name, note)
    if not tags:
        return False
    prefixes = [p for p in active_prefixes if p]
    if not prefixes:
        return False
    postfix_words = _recognized_postfix_words(
        auto_expand_postfix=auto_expand_postfix,
        suppress_postfix=suppress_postfix,
        deprecated_postfix=deprecated_postfix,
    )
    # --suppress / --expand are PRESENTATION postfixes (a lens carve-out /
    # auto-expansion), NOT curation acts: they must not move the curation
    # cursor. --deprecated IS a curation act (deliberate triage) and still
    # counts, as does every plain / middle-segment variant of the family.
    presentation_postfixes = {
        w
        for w in (
            str(auto_expand_postfix or "").strip(),
            str(suppress_postfix or "").strip(),
        )
        if w
    }
    for t in tags:
        for prefix in prefixes:
            m = slice_tag_variant_match(t, prefix, postfix_words=postfix_words)
            if m is None:
                continue
            if m["postfix"] in presentation_postfixes:
                continue  # presentation-only variant: not a curation act
            return True
    return False


# The maximum number of OR-terms (positive prefix clauses + negation tags)
# allowed in a compiled Workflowy search string before we REFUSE to build it.
# See compile_slice_search_expression's docstring for WHY a cap is unavoidable
# (Workflowy has no wildcard, so every discovered middle-segment deprecated
# variant must be enumerated as its own negation -> the term count can explode).
# 80 mirrors the read_uuid_from_symbol candidate cap used elsewhere in this
# codebase; it is a display/GUI-sync guard only and never affects the
# server-side membership filter (which is computed by is_slice_member over the
# cache, independent of this string).
SLICE_SEARCH_MAX_TERMS = 80


# @beacon[
#   id=f8policy@collect-deprecated-variant-tags,
#   slice_labels=slices,
#   kind=ast,
#   comment=Scan the /nodes-export cache subtree under root_uuid for the DEPRECATED MIDDLE-SEGMENT variant tags of each active prefix (via the shared slice_tag_variant_match), returning {prefix: [dep_variant_tag, ...]}. Feeds compile_slice_search_expression so the wildcard-less Workflowy search can enumerate each P<middle>--deprecated as its own negation -- the server discovers them; the compiler lists them. Pure over the cache; empty map when cache unavailable. NOT needed by the server-side FILTER (is_slice_member reasons over the cache directly) -- this exists ONLY to make the live-GUI-sync search string reproduce the same member set.,
# ]
def collect_deprecated_variant_tags(
    cache_by_id: dict[str, dict[str, Any]] | None,
    root_uuid: str,
    active_prefixes: Iterable[str],
    *,
    deprecated_postfix: str = DEFAULT_DEPRECATED_POSTFIX,
    auto_expand_postfix: str = DEFAULT_AUTO_EXPAND_POSTFIX,
    suppress_postfix: str = DEFAULT_SUPPRESS_POSTFIX,
) -> dict[str, list[str]]:
    """Scan the cache subtree for the deprecated MIDDLE-SEGMENT variant tags.

    For each active base prefix ``P``, returns the SORTED, de-duped list of
    distinct ``'#'``-stripped tags actually present anywhere under ``root_uuid``
    that match ``P`` with the deprecated postfix under the wildcard rule --
    i.e. every ``P``, ``P--deprecated``, ``P-<middle>--deprecated`` variant
    that a human's positive ``#P`` search would ALSO surface and therefore must
    be explicitly negated to reproduce ``is_slice_member``.

    Returns ``{prefix: [dep_variant_tag, ...]}``. A prefix with no deprecated
    variants under the root maps to ``[]`` (its clause just needs the bare
    ``-#P--deprecated`` guard the base case already supplies). Returns an empty
    per-prefix map when the cache is unavailable (caller falls back to the
    base per-prefix negation only).

    WHY THIS IS NEEDED (the explosion): Workflowy search has NO wildcard, and a
    positive ``#P`` term matches EVERY ``P<middle>--deprecated`` by substring.
    A negation binds ONLY to its immediately-preceding tag, so to keep those
    deprecated variants OUT under the ``#P`` clause we must list each one as
    its own ``-#<variant>`` after ``#P``. The server discovers them from the
    cache here; the compiler enumerates them. (The server-side FILTER never
    needs this -- ``is_slice_member`` reasons over the cache directly.)
    """
    out: dict[str, list[str]] = {}
    prefixes = [str(p).strip() for p in active_prefixes if str(p).strip()]
    for p in prefixes:
        out[p] = []
    if not cache_by_id or not root_uuid or root_uuid not in cache_by_id or not prefixes:
        return out

    postfix_words = _recognized_postfix_words(
        auto_expand_postfix=auto_expand_postfix,
        suppress_postfix=suppress_postfix,
        deprecated_postfix=deprecated_postfix,
    )

    # children index by parent_id (same idiom as build_slice_subtree_from_cache).
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for node in cache_by_id.values():
        pid = node.get("parent_id")
        if pid is not None:
            children_by_parent.setdefault(str(pid), []).append(node)

    seen_per_prefix: dict[str, set[str]] = {p: set() for p in prefixes}

    def visit(nid: str) -> None:
        node = cache_by_id.get(nid)
        if node is None:
            return
        for tag in extract_node_tags(node.get("name"), node.get("note")):
            for p in prefixes:
                m = slice_tag_variant_match(tag, p, postfix_words=postfix_words)
                if m is not None and m["postfix"] == deprecated_postfix:
                    seen_per_prefix[p].add(tag)
        for ch in children_by_parent.get(nid, []):
            cid = _node_id(ch)
            if cid:
                visit(cid)

    visit(root_uuid)
    for p in prefixes:
        out[p] = sorted(seen_per_prefix[p])
    return out


# @beacon[
#   id=auto-beacon@compile_slice_search_expression-3kww,
#   role=compile_slice_search_expression,
#   slice_labels=slices,
#   kind=ast,
# ]
def compile_slice_search_expression(
    active_prefixes: Iterable[str],
    *,
    deprecated_postfix: str = DEFAULT_DEPRECATED_POSTFIX,
    deprecated_variants_by_prefix: dict[str, list[str]] | None = None,
    max_terms: int = SLICE_SEARCH_MAX_TERMS,
) -> dict[str, Any]:
    """Compile the Workflowy membership search string for a slice group.

    Returns a BUNDLE (not a bare string), so the too-many-variants refusal can
    ride alongside the string without a second return channel::

        {
          "search_string":     str | None,   # None iff too_many_variants
          "too_many_variants": bool,
          "term_count":        int,          # total emitted OR-terms/negations
          "max_terms":         int,
        }

    Emits, per active prefix P, a clause ``#P -#P--deprecated [ -#<variant> ...]``
    (all discovered deprecated MIDDLE-SEGMENT variants of P negated), the
    clauses joined by ``OR``::

        #tag--a -#tag--a--deprecated OR #tag--b -#tag--b--deprecated

    NO parentheses (Workflowy treats parens as raw-text search and breaks tag
    matching). Negation binds to the immediately-preceding tag, so each prefix
    carries its own deprecated-negation(s) -- exactly the per-prefix membership
    semantics of ``is_slice_member`` (one live prefix keeps the node in).
    Emit order is cosmetic (Workflowy OR is commutative); we keep stored
    prefix order. The '#' is re-added here (stored prefixes are '#'-less).

    MIDDLE-SEGMENT EXPLOSION + THE CAP. With the
    middle-segment wildcard, a positive ``#P`` term matches EVERY
    ``P<middle>--deprecated`` variant by substring, and a negation binds only
    to its immediately-preceding tag -- so P's clause must negate EACH such
    discovered variant individually (``#P -#P--deprecated -#P-x--deprecated
    -#P-y--deprecated ...``). That is a genuine term explosion bounded by the
    number of distinct deprecated variants under the root, and Workflowy has
    NO wildcard to collapse it. When the total term count exceeds ``max_terms``
    we REFUSE: return ``search_string=None`` + ``too_many_variants=True``. The
    caller then does NOT populate Workflowy's search box and surfaces a clear
    message; the RENDER is unaffected because the membership filter is computed
    by ``is_slice_member`` over the cache, NOT by this string.

    ``deprecated_variants_by_prefix`` (``{prefix: [dep_variant_tag, ...]}``,
    from :func:`collect_deprecated_variant_tags`) supplies the discovered
    variants. When None (no cache handy / pure-string callers / the self-test),
    we fall back to the base ``-#P--deprecated`` negation ONLY -- correct for
    the no-middle-variant case and byte-identical to the historical output.

    ⚠ THREE-WAY EQUIVALENCE (the load-bearing invariant): the RENDERABLE
    compiled string, ``is_slice_member``, and a human's pasted search MUST all
    compute the IDENTICAL member set. The per-prefix ``-#P--deprecated`` guard
    plus every discovered ``-#<variant>`` negation is exactly the membership
    exclusion ``is_slice_member`` applies (a node whose every matched prefix is
    deprecated -- via bare OR middle-segment deprecated tag -- is excluded).
    Do NOT confuse this with Phase-2 ``collapse_deprecated`` (PRESENTATION):
    Phase 2 collapses subtrees of deprecated nodes that ARE members; membership
    exclusion here is orthogonal and both are needed. (When we REFUSE over the
    cap, three-way equivalence is not violated -- it is simply not offered for
    the GUI, and the server-side filter remains authoritative.)
    """
    dep_suffix = POSTFIX_SEP + deprecated_postfix
    prefixes = [p for p in active_prefixes if p]
    term_count = 0
    clauses: list[str] = []
    for prefix in prefixes:
        # Positive term + the base deprecated negation (always present, exactly
        # as the historical output).
        parts = [f"#{prefix}", f"-#{prefix}{dep_suffix}"]
        # Plus every DISCOVERED middle-segment deprecated variant of this
        # prefix (de-duped against the base negation we already emitted).
        base_dep = f"{prefix}{dep_suffix}"
        variants = (deprecated_variants_by_prefix or {}).get(prefix, []) or []
        for v in variants:
            if v and v != base_dep:
                parts.append(f"-#{v}")
        term_count += len(parts)
        clauses.append(" ".join(parts))

    if term_count > max_terms:
        return {
            "search_string": None,
            "too_many_variants": True,
            "term_count": term_count,
            "max_terms": max_terms,
        }
    return {
        "search_string": " OR ".join(clauses),
        "too_many_variants": False,
        "term_count": term_count,
        "max_terms": max_terms,
    }


# ---------------------------------------------------------------------------
# Per-node tag classification.
# ---------------------------------------------------------------------------

# @beacon[
#   id=f8policy@classify-node-tags,
#   slice_labels=slices,
#   kind=ast,
#   comment=Classify ONE node's NAME tags against the active slice config -> flags has_active / has_auto / has_suppress / has_deprecated / has_global_never_expand. The atomic per-node primitive feeding membership (Phase 1) and the plan (Phase 2). match_scope (keyword, default 'name_and_note') governs WHERE the family-postfix flags read: under 'name' (the 084 ruling) they read ONLY the name's TRAILING tag region via the core splitter -- a mid-title or note-authored --expand/--suppress/--deprecated is prose, not an operation; under 'name_and_note' they read name+note (Workflowy-search parity, the prior behavior). The global-never flag is TRAILING-REGION-ONLY under EVERY scope (Dan's 084 D1: an absolute structural control must be a deliberate trailing tag) via the ONE shared helper pair (normalize_global_never_stems + name_has_global_never_tag).,
# ]
def classify_node_tags(
    name: Any,
    active_prefixes: Iterable[str],
    *,
    note: Any = None,
    auto_expand_postfix: str = DEFAULT_AUTO_EXPAND_POSTFIX,
    suppress_postfix: str = DEFAULT_SUPPRESS_POSTFIX,
    deprecated_postfix: str = DEFAULT_DEPRECATED_POSTFIX,
    global_never_expand_tags: Iterable[str] = (),
    match_scope: str = "name_and_note",
) -> dict[str, bool]:
    """Classify a single node's NAME-field tags against the active slice.

    Returns a flags dict::

        {
          "has_active":              any tag matches an active prefix,
          "has_auto":                any active tag has the auto-expand postfix,
          "has_suppress":            any active tag has the suppress postfix,
          "has_deprecated":          any active tag has the deprecated postfix,
          "has_global_never_expand": any TRAILING NAME tag is in the
                                      global-never-expand list (see below),
        }

    "active tag has postfix X" means a tag matching prefix ``<active>--X``
    (literal startsWith), so further-postfixed variants still count.

    ``match_scope`` is the SELECTION-stage scope knob (keyword-only, the same
    one :func:`is_slice_member` takes) and now governs WHERE the
    active-family flags read (084 ruling):
      * ``"name_and_note"`` (default -- prior behavior) reads tags from the
        NAME *and* NOTE via ``extract_node_tags``: a note-authored
        ``--expand``/``--suppress``/``--deprecated`` still classifies
        (Workflowy-search parity for operation tags).
      * ``"name"`` reads ONLY the name's TRAILING tag region, via the core
        splitter (``_core_trailing_name_tags``). A ``#tag`` mentioned
        mid-title as prose, or living only in the note body, is prose -- it
        does NOT classify as an operation.
    The global-never flag is TRAILING-REGION-ONLY under EVERY scope (Dan's
    084 D1 ruling): an absolute structural control must be a deliberate,
    visible TRAILING NAME tag -- a mid-title or note-only ``#wfy-suppress``
    never suppresses -- via the ONE shared helper pair
    (normalize_global_never_stems + name_has_global_never_tag).
    """
    active_prefixes = list(active_prefixes)
    # GLOBAL-NEVER DOCTRINE (TRAILING REGION, Dan 2026-07 post-IMPPTX audit +
    # 084 D1 tightening): the global-never flag reads ONLY the name's
    # TRAILING tag region via the ONE shared helper pair
    # (normalize_global_never_stems + name_has_global_never_tag) -- it is an
    # ABSOLUTE STRUCTURAL control, so a tag buried in a note body OR wedged
    # mid-title never suppresses, whatever the scope. The active-family flags
    # below follow match_scope: 'name_and_note' keeps the parity reading
    # (extract_node_tags over NAME+NOTE); 'name' reads the trailing region
    # only -- a note-authored --expand/--suppress/--deprecated is prose there.
    global_stems = normalize_global_never_stems(global_never_expand_tags)
    # MIDDLE-SEGMENT WILDCARD (Dan, July 2026): every flag is now derived from
    # the ONE shared matcher (slice_tag_variant_match), so a middle-segment
    # variant of a slice tag classifies as whatever type its trailing postfix
    # (or absence of one) says -- e.g. comm-jim-teams--deprecated sets
    # has_active + has_deprecated on prefix comm-jim; comm-jim-teams (no
    # postfix) sets has_active only (plain family member). The competing-tag
    # POLICY that consumes these flags (Phase 2: suppress > expand > deprecated,
    # etc.) is UNCHANGED -- only the SET of tags that feed the flags widened to
    # include middle-segment variants. Recognizing the postfix set once per tag
    # (not per-variant-prefix startswith) is the single-recognizer discipline.
    postfix_words = _recognized_postfix_words(
        auto_expand_postfix=auto_expand_postfix,
        suppress_postfix=suppress_postfix,
        deprecated_postfix=deprecated_postfix,
    )

    has_active = False
    has_auto = False
    has_suppress = False
    has_deprecated = False
    has_global = name_has_global_never_tag(name, global_stems)

    # SCOPE (084): under 'name' the family-postfix flags read the name's
    # TRAILING tag region only -- the SAME reader is_slice_member uses, so
    # membership and classification can never disagree about where a tag
    # lives. Any unrecognized scope falls safe to the broader name+note
    # harvest (prior behavior).
    if match_scope == "name":
        harvest = _core_trailing_name_tags(name)
    else:
        harvest = extract_node_tags(name, note)

    for tag in harvest:
        # Match this tag against each active prefix via the shared wildcard
        # matcher; the FIRST matching prefix determines its classification
        # (a tag can only sensibly belong to one base family). Its recognized
        # trailing postfix (if any) sets the auto/suppress/deprecated flag.
        for prefix in active_prefixes:
            if not prefix:
                continue
            m = slice_tag_variant_match(tag, prefix, postfix_words=postfix_words)
            if m is None:
                continue
            has_active = True
            pf = m["postfix"]
            if pf == auto_expand_postfix:
                has_auto = True
            elif pf == suppress_postfix:
                has_suppress = True
            elif pf == deprecated_postfix:
                has_deprecated = True
            break  # classified against its base family; stop at first match.

    return {
        "has_active": has_active,
        "has_auto": has_auto,
        "has_suppress": has_suppress,
        "has_deprecated": has_deprecated,
        "has_global_never_expand": has_global,
    }


# Sentinel id values that mean "this node has NO real Workflowy UUID" and must
# be treated as empty/transparent. The Workflowy "Home" mainTreeRoot wrapper is
# the canonical case: its DOM projectid comes through as the literal STRING
# "None" (Python str(None) round-tripped, or a "null"/"undefined" stringified by
# the client), NOT an empty string. Without this, str("None") is truthy and the
# empty-id transparency guards (in _flatten / build_expansion_plan.annotate) do
# NOT fire, so Home is treated as a real node, missed in the cache, and trips a
# false visible_tree_cache_mismatch. Normalizing these sentinels to "" makes
# the Home wrapper transparent regardless of how the DOM attribute stringifies.
_EMPTY_ID_SENTINELS = frozenset({"", "none", "null", "undefined"})


def _node_id(node: dict[str, Any]) -> str:
    """Return a node's real UUID, or "" for a sentinel/absent id.

    A node with no real Workflowy UUID (e.g. the synthetic "Home" root, whose
    projectid arrives as the literal string "None"/"null") normalizes to "" so
    the empty-id transparency handling treats it as a non-node container.
    """
    raw = node.get("id") or node.get("uuid") or ""
    s = str(raw).strip()
    if s.lower() in _EMPTY_ID_SENTINELS:
        return ""
    return s


def _children(node: dict[str, Any]) -> list[dict[str, Any]]:
    kids = node.get("children")
    return kids if isinstance(kids, list) else []


# ===========================================================================
# COMPLETED-NODE EXCLUSION POLICY (server-side GLIMPSE only) --- Dan, 2026
# ===========================================================================
#
# POLICY (default TRUE = we ALWAYS exclude completed; still not exposed as an
# MCP `glimpse()` tool argument): any SERVER-SIDE (cache-projected) GLIMPSE --
# i.e. any GLIMPSE that sources its node set from the /nodes-export cache
# because a slice_group and/or a root_uuid was supplied, whether it RETURNS to
# the agent or is PUSHED to the docked modal -- EXCLUDES nodes marked completed
# in Workflowy. A CLIENT-SIDE (live-DOM) GLIMPSE is never filtered here (it
# faithfully mirrors whatever the user has expanded in the GUI, including their
# own show/hide-completed choice), and by design NEVER will be, even if this
# policy is later promoted to a real flag -- so this exclusion lives ONLY on
# the cache path.
#
# SPEC-HOIST (NXSTAGE, 2026): this is a STAGE-1 admission policy, and it is NO
# LONGER a hardcoded constant threaded at each call site. Every server-side
# STAGE-1 build now READS exclude_completed OFF its RenderProjectionSpec
# (RenderProjectionSpec.exclude_completed, whose default IS
# SERVER_SIDE_GLIMPSE_EXCLUDE_COMPLETED via the normalizer). The spec is
# constructed BEFORE the STAGE-1 build in all callers -- build_stage1_base_tree
# (_glimpse_slice_group_from_cache + _handle_render_slice_modal's slice cell),
# build_slice_subtree_from_cache(member_all) (_glimpse_root_uuid_from_cache +
# the modal raw-dump cell), and build_selected_subtree_from_cache
# (_lightning_rod_from_cache / _lightning_rod_render_symbols + the modal
# lightning cell) -- and the value is still passed LOOSE into the build (the
# funnel signatures are unchanged this pass). Net: same behavior (default
# True), but a future client can flow a different value through the spec.
#
# DOM-PATH CONFIRMED (NXSTAGE): the CLIENT-SIDE (live-DOM) GLIMPSE path does
# NOT run any STAGE-1 build and does NOT read the cache for membership -- it is
# handed the human's already-expanded UUID set and only VERIFIES those UUIDs
# exist in the cache via validate_subtree_against_cache, which is UUID-
# membership ONLY (no content check, no parent-edge check, and specifically NO
# completed check). So exclude_completed is simply N/A on the DOM path, and
# WYSIWYG holds -- a completed node the human has expanded still renders.
# Verified: no `completed` filtering exists anywhere in validate_subtree_
# against_cache / _flatten.
#
# WHY THE FILTER LIVES IN PHASE 1 (build_slice_subtree_from_cache) AND NOWHERE
# ELSE: the exclusion is an ADMIT-TIME prune during the top-down collect() DFS.
# Because a completed node is dropped before it is ever admitted, the whole
# Phase 2/3/4 machinery (expand / suppress / deprecated postfixes, the
# expansion plan, the render composer) never sees it -- so "a completed
# ancestor hides its entire subtree, and NO --expand postfix can un-hide it"
# is true BY CONSTRUCTION rather than by an extra rule bolted onto a later
# phase. This is the single, minimal interjection point; do not replicate it
# downstream.
#
# THE COMPLETED-ROOT EDGE CASE (deliberately NOT special-cased): the collect()
# DFS enters at root_uuid unconditionally and only tests the completed flag
# when deciding whether to RECURSE INTO A CHILD. Consequently a completed
# root_uuid is itself included and its (non-completed) descendants render
# normally -- exactly as if the root were not completed. Dan's explicit ruling:
# this is the desired behavior (targeting a completed node as the root to
# inspect its subtree is a legitimate use case) AND it keeps the code branch-
# free (no special root check). Accepted by design.


def _node_is_completed(node: dict[str, Any]) -> bool:
    """True iff a /nodes-export cache node is marked completed in Workflowy.

    The cache node carries a boolean ``completed`` field (and a companion
    ``completedAt`` epoch, null when not completed). We treat the node as
    completed iff ``completed`` is truthy OR ``completedAt`` is a non-null
    value -- either signal alone is sufficient, so a future export that
    populates only one of them still reads correctly. Pure + defensive: a
    missing/None/absent node reads as NOT completed (fail-open: when unsure we
    do not hide content).
    """
    if not isinstance(node, dict):
        return False
    if bool(node.get("completed")):
        return True
    return node.get("completedAt") is not None


# @beacon[
#   id=f8policy@build-anchor-boundary-predicate,
#   slice_labels=slices,nexus--anchors,
#   kind=ast,
#   comment=Build the STOP-RECURSION boundary predicate for the anchor-points projection: a pure (node)->bool that is True iff the node is an anchor (its NAME trailing-tag region carries any configured base anchor tag -- bare, or with ONE registered attribute word: local OR favorite). Routes through the shared classify_anchor_tags matcher on is_anchor -- so a LOCAL anchor (#wfy-ns--local) and a FAVORITE anchor (#wfy-ns--fav) STILL bound a 'descendants' GLIMPSE exactly like a bare-base one (I10: favorite has no bearing on descent; locality only hides an anchor from list_anchors, never from the recursion boundary). A --deprecated-ONLY node is NOT an anchor (I4) and therefore does NOT bound. Reads the NAME's TRAILING tag region via the core splitter (_core_trailing_name_tags -> nexus_map_codebase.split_name_and_tags), the SAME reader _anchor_dock_root_guard / list_note_sessions / the anchor tooling use, so 'what is an anchor' cannot drift between this traversal boundary, the enumeration, and the anchor tooling (and a mid-title prose '#wfy-ns' is NOT a boundary). Anchor tags are matched EXACTLY (no wildcard middle -- wfy-ns !-> wfy-nsx), unlike slice prefixes. Passed as build_selected_subtree_from_cache(boundary_predicate=...) so a raw-root / lightning projection stops descending at child anchors while still emitting the child-anchor node itself as a visible leaf signpost. Empty/blank anchor-tag list -> a predicate that is always False (no boundaries). Pure; no I/O.,
# ]
def build_anchor_boundary_predicate(
    anchor_tags: Iterable[str],
    *,
    local_postfixes: "Iterable[str] | None" = None,
    favorite_postfixes: "Iterable[str] | None" = None,
    deprecated_postfixes: "Iterable[str] | None" = None,
) -> "Callable[[dict[str, Any]], bool]":
    """Build a STOP-RECURSION boundary predicate that fires on a CHILD ANCHOR.

    Returns a pure ``(node) -> bool`` suitable directly as the
    ``boundary_predicate`` of :func:`build_selected_subtree_from_cache`. The
    predicate is True iff the node is an ANCHOR -- its NAME trailing-tag region
    carries ANY configured base anchor tag (``anchor_tags`` /
    ``note_session_tags``, a LIST) EITHER bare (``base``) OR with ONE
    registered attribute word (``base<SEP><local-word>`` or
    ``base<SEP><favorite-word>``). A ``--deprecated``-ONLY node is NOT an
    anchor (I4) and does NOT bound.

    ⭐ LOCAL AND FAVORITE ANCHORS STILL BOUND DESCENT (I10: favorite has no
    bearing on descent -- a Dock is a Dock). A local anchor (e.g.
    ``#wfy-ns--local``) or a favorite (``#wfy-ns--fav``) is a FULLY-REAL
    anchor; locality/favoriteness are LISTING concerns only, decided in
    ``list_anchors``. So this predicate keys off ``is_anchor``, NOT
    ``is_anchor and not is_local``.

    Both this boundary and ``list_anchors`` route through the ONE shared
    :func:`classify_anchor_tags` matcher. Both read the NAME's TRAILING tag
    region via the core splitter (``_core_trailing_name_tags`` ->
    ``nexus_map_codebase.split_name_and_tags``) -- the SAME reader the Dock
    root guard (``_anchor_dock_root_guard``) and the enumeration
    (``list_note_sessions``) use -- so Dock identity is uniformly
    trailing-only and 'what is an anchor' can never drift between the recursion
    boundary and the enumeration. (A mid-title prose ``#wfy-ns`` is NOT a
    boundary; this closes the divergence where this predicate alone read the
    title anywhere-in-name.) Matching is EXACT-base (no wildcard middle),
    NOT prefix/family: ``wfy-ns`` never bleeds into ``wfy-nsx``.

    ``local_postfixes`` / ``favorite_postfixes`` / ``deprecated_postfixes``
    are the configured attribute words (each default ``None`` -> the shipped
    default list); they only widen WHICH tags count as an anchor -- they never
    change the always-bound behavior.

    An empty / blank / all-whitespace ``anchor_tags`` yields a predicate that
    is ALWAYS False (there are no anchor boundaries), so a caller that could
    not read the config degrades to an ordinary raw dump rather than erroring.
    Pure; no I/O.
    """
    bases = [
        _strip_hash(t) for t in (anchor_tags or []) if isinstance(t, str) and t.strip()
    ]
    bases = [b for b in bases if b]
    if not bases:
        return lambda node: False  # noqa: E731  (no anchor tags -> no boundaries)

    _local = (
        list(local_postfixes)
        if local_postfixes is not None
        else [DEFAULT_LOCAL_ANCHOR_POSTFIX]
    )
    _favorite = (
        list(favorite_postfixes)
        if favorite_postfixes is not None
        else [DEFAULT_FAVORITE_ANCHOR_POSTFIX]
    )
    _deprecated = (
        list(deprecated_postfixes)
        if deprecated_postfixes is not None
        else [DEFAULT_DEPRECATED_ANCHOR_POSTFIX]
    )

    def _is_anchor_boundary(node: dict[str, Any]) -> bool:
        if not isinstance(node, dict):
            return False
        verdict = classify_anchor_tags(
            _core_trailing_name_tags(node.get("name")),
            bases,
            local_postfixes=_local,
            favorite_postfixes=_favorite,
            deprecated_postfixes=_deprecated,
        )
        # is_anchor (bare / local / favorite): every proper family form is a
        # boundary; a --deprecated-only node is not an anchor and does not bound.
        return bool(verdict["is_anchor"])

    return _is_anchor_boundary


# ===========================================================================
# PHASE 1 — predicate-driven minimal-subtree builder (cache-only)
# ===========================================================================

# @beacon[
#   id=f8policy@phase1-build-subtree,
#   slice_labels=slices,
#   kind=ast,
#   comment=THE generic Stage-1 SELECTION primitive: walk the /nodes-export cache subtree under root_uuid, admit a node iff member_predicate(node) is True, and build the minimal connective subtree (every admitted node + all ancestors up to and including root_uuid, children priority-ordered). Returns a nested {id,name,note,children} root, or None if root_uuid is absent from the cache. Every cache-sourced selection source converges here by supplying its own admit predicate over ONE tree-walk machinery: slice groups (the is_slice_member closure, via the build_slice_subtree_from_cache wrapper), the raw-dump root GLIMPSE (member_all -> lambda node: True), and Lightning Rod tag/text-search predicates. Two orthogonal TRAVERSAL policies ride alongside the membership predicate: exclude_completed prunes a completed CHILD at admit time (its whole subtree becomes invisible; no downstream --expand can un-hide it), and boundary_predicate is a stop-recursion boundary (a child satisfying it is still visited/admitted and anchors the connective spine, but the walk does not descend into it -- it renders as a leaf). Both policies exempt the root by construction (each is tested only when deciding whether to descend into a CHILD). A THIRD traversal policy, max_depth (ap:EPR86W part a; the spec's recursive=false maps to 1): the walk descends only while the CURRENT node's depth < max_depth (root = depth 0), so max_depth=1 renders the root + its DIRECT children and nothing deeper -- child Docks and plain nodes alike; descend_policy is moot under the cap (the cap stops the descent first). The same gate stops the materialize recursion, so a capped node is childless in the output by construction. None (the default) is the ordinary unbounded walk -- byte-for-byte prior behavior; only the raw-root surfaces thread the cap (a membership selection is unbounded by construction and refuses recursive=false loudly at the spec normalizer).,
# ]
def build_selected_subtree_from_cache(
    cache_by_id: dict[str, dict[str, Any]],
    root_uuid: str,
    member_predicate: "Callable[[dict[str, Any]], bool]",
    *,
    exclude_completed: bool = False,
    boundary_predicate: "Callable[[dict[str, Any]], bool] | None" = None,
    max_depth: int | None = None,
) -> dict[str, Any] | None:
    """Generic Phase-1 SELECTION: minimal connective subtree by a predicate.

    THE universal Stage-1 (SELECTION) primitive. Walk the /nodes-export cache
    subtree under ``root_uuid``, ADMIT a node iff ``member_predicate(node)`` is
    True, then build the minimal connective subtree (every admitted node + all
    ancestors up to and including ``root_uuid``, children priority-ordered).
    Returns a nested ``{id,name,note,children:[...]}`` root, or None if
    ``root_uuid`` is absent from the cache. The returned subtree is the SAME
    shape Phase 0/2 consume, so every selection source converges here.

    Each caller supplies a different admit predicate but reuses ONE tree-walk
    machinery (children index -> collect -> keep -> materialize):
      * slice groups          -> ``is_slice_member`` closure (via the
        ``build_slice_subtree_from_cache`` wrapper below).
      * raw-dump root GLIMPSE  -> ``lambda node: True`` (via ``member_all`` in
        the wrapper below).
      * Lightning Rod tags     -> a prefix-family tag predicate.
      * Lightning Rod search   -> a free-text/regex query predicate.

    ``member_predicate`` is a pure ``(node) -> bool`` over the cache node dict
    (which carries ``id``/``name``/``note``/``parent_id``/``priority``). It is
    a MEMBERSHIP test only.

    ``exclude_completed`` (default False) is a distinct TRAVERSAL policy, NOT
    folded into the predicate: the top-down ``collect`` DFS does NOT recurse
    into a completed CHILD, so a completed ancestor hides its entire subtree at
    ADMIT time (before Phase 2/3), and no downstream ``--expand`` postfix can
    un-hide it. The ROOT is exempt by construction: ``collect`` enters at
    ``root_uuid`` unconditionally and only tests the flag when deciding whether
    to descend into a CHILD, so a completed ``root_uuid`` is itself included and
    its non-completed descendants render normally.

    ``boundary_predicate`` (default None) is a SECOND distinct traversal policy,
    orthogonal to both ``member_predicate`` (which decides admission) and
    ``exclude_completed`` (which HIDES). When a CHILD satisfies it, that child
    is a STOP-RECURSION BOUNDARY: it is still visited (admitted iff
    ``member_predicate`` also holds, and it can anchor the connective spine like
    any other node), but the walk does NOT descend into its subtree -- the
    boundary node renders as a LEAF, keeping its own descendants out of this
    projection ('prune AT the boundary, keep the boundary visible', NOT
    'suppress the boundary and everything under it'). Like ``exclude_completed``
    the ROOT is exempt (the test is applied only when deciding whether to
    descend into a CHILD), so pointing this at a node that is itself a boundary
    still renders that node + its immediate children up to the next boundary.
    The same boundary set also stops the ``materialize`` recursion, so a
    boundary is guaranteed childless in the output even if a deeper descendant
    would otherwise re-attach via the ancestor-keep pass.

    ``max_depth`` (default None) is a THIRD distinct traversal policy, the
    depth-cap leg of the ``recursive`` traversal flag ([ap:EPR86W] part a; the
    spec's ``recursive=False`` maps to ``max_depth=1``). The walk descends only
    while the CURRENT node's depth is below the cap (the root is depth 0), so
    ``max_depth=1`` renders the root plus its DIRECT children and nothing
    deeper -- child Docks and plain nodes alike, exactly ``dock.peek``'s
    ``recursive=False`` semantics (``descend_policy`` is moot under the cap:
    the depth gate stops the descent before any boundary could). The same gate
    stops the ``materialize`` recursion, so a capped node is childless in the
    output even if a deeper descendant were marked 'keep' via another path.
    Like the other two policies the ROOT is exempt by construction (the gate
    is tested when deciding whether to descend FROM a node, never on the root
    itself). None (the default) is the ordinary unbounded walk -- byte-for-byte
    prior behavior. Only the RAW-ROOT surfaces thread a cap today (dock.render,
    glimpse.run root_handle, the estate {dock} form, and the push/modal path
    that re-projects their spec); a membership selection (slice groups, lenses,
    Lightning Rod) is unbounded by construction and refuses ``recursive=False``
    loudly at the spec normalizer.
    """
    if root_uuid not in cache_by_id:
        return None

    # children index by parent_id (priority-sorted for stable document order).
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for node in cache_by_id.values():
        pid = node.get("parent_id")
        if pid is not None:
            children_by_parent.setdefault(str(pid), []).append(node)
    for kids in children_by_parent.values():
        kids.sort(key=lambda n: (n.get("priority") if isinstance(n.get("priority"), (int, float)) else 0))

    # 1. DFS from root collecting member ids (and remember every node visited).
    member_ids: set[str] = set()
    visited_order: list[str] = []
    # STOP-RECURSION BOUNDARY set: children that satisfy boundary_predicate.
    # They are still VISITED (admitted iff member_predicate holds) but we do
    # NOT descend below them, and the same set stops the materialize recursion
    # so a boundary node is rendered as a leaf even if some descendant would
    # otherwise have been kept as an independent member. Empty when no
    # boundary_predicate is supplied.
    boundary_ids: set[str] = set()

    def collect(nid: str, depth: int = 0) -> None:
        node = cache_by_id.get(nid)
        if node is None:
            return
        visited_order.append(nid)
        if member_predicate(node):
            member_ids.add(nid)
        # If THIS node is a stop-recursion boundary (recorded by its parent
        # when it was reached below), do not descend into its children. The
        # root is never in boundary_ids (only children are tested), so a
        # boundary root still renders its immediate subtree -- root-exempt like
        # the completed policy.
        if nid in boundary_ids:
            return
        # DEPTH CAP (ap:EPR86W part a; recursive=False -> max_depth=1). Tested
        # when deciding whether to descend FROM a node (root = depth 0), so the
        # root itself is exempt by construction -- the same root-exempt shape
        # as the completed and boundary policies. Under the cap neither the
        # completed test nor the boundary test below is reached for a capped
        # node's children: the walk simply stops. The cap is the STRONGER
        # policy: descend_policy is moot under recursive=False, exactly as on
        # dock.peek.
        if max_depth is not None and depth >= max_depth:
            return
        for ch in children_by_parent.get(nid, []):
            cid = _node_id(ch)
            if not cid:
                continue
            # COMPLETED-NODE EXCLUSION (default off). Prune at ADMIT time: skip
            # a completed CHILD entirely -- it is neither collected as a member
            # nor descended into, so its whole subtree is invisible and no
            # downstream --expand postfix can un-hide it (Phase 2/3 never see
            # it). Tested on the child (not the current node) so the ROOT is
            # exempt: collect() was entered at root_uuid unconditionally, so a
            # completed root is itself included and its non-completed
            # descendants render.
            if exclude_completed and _node_is_completed(cache_by_id.get(cid)):
                continue
            # STOP-RECURSION BOUNDARY (traversal policy; default off). Tested on
            # the CHILD so the root is exempt by construction. A boundary child
            # is recorded and STILL descended into via collect() -- collect()
            # visits (and possibly admits) the boundary node itself, then
            # returns early WITHOUT recursing into ITS children (see the
            # `nid in boundary_ids` guard above). So the boundary node is a
            # visible signpost; its descendants are pruned from this projection.
            if boundary_predicate is not None and boundary_predicate(ch):
                boundary_ids.add(cid)
            collect(cid, depth + 1)

    collect(root_uuid)

    # 2. Mark every ancestor of a member (up to root) as "keep".
    keep: set[str] = set()
    for mid in member_ids:
        cur: str | None = mid
        guard = 0
        while cur and cur not in keep and guard < 4096:
            keep.add(cur)
            if cur == root_uuid:
                break
            node = cache_by_id.get(cur)
            cur = str(node.get("parent_id")) if node and node.get("parent_id") is not None else None
            guard += 1
    keep.add(root_uuid)  # root always kept as the connective anchor

    # 3. Materialize the pruned nested subtree (only kept nodes).
    def materialize(nid: str, depth: int = 0) -> dict[str, Any] | None:
        node = cache_by_id.get(nid)
        if node is None or nid not in keep:
            return None
        out: dict[str, Any] = {
            "id": nid,
            "name": node.get("name"),
            "note": node.get("note"),
            "children": [],
        }
        # STOP-RECURSION BOUNDARY: a boundary node materializes as a LEAF. We
        # did not descend into it during collect(), so nothing under it is a
        # member; stopping the materialize recursion at the boundary too
        # guarantees a boundary is ALWAYS childless in the output even if a
        # deeper descendant were marked 'keep' via another path.
        if nid in boundary_ids:
            return out
        # DEPTH CAP: same gate as collect(), so a depth-capped node is ALWAYS
        # childless in the output (mirrors the boundary guarantee).
        if max_depth is not None and depth >= max_depth:
            return out
        for ch in children_by_parent.get(nid, []):
            cid = _node_id(ch)
            child = materialize(cid, depth + 1) if cid else None
            if child is not None:
                out["children"].append(child)
        return out

    return materialize(root_uuid)


# @beacon[
#   id=f8policy@build-selected-subtree-from-forest,
#   slice_labels=nexus--md-preview,slices,
#   kind=ast,
#   comment=SCOPED LIGHTNING ROD (ap:LRSCOPE) — the SECOND selection substrate. The forest-walking SIBLING of build_selected_subtree_from_cache: same admit-by-member_predicate + minimal-connective-subtree + optional stop-recursion boundary contract and the SAME {id,name,note,children} output shape, but it iterates a PROVIDED in-memory forest (the frozen-DOM captured tree) instead of walking the /nodes-export cache from a root. Reuses the SINGULAR predicates (build_tag_predicate / build_search_predicate) verbatim — predicates are substrate-agnostic (node)->bool, so there is NO predicate duplication. It is a PURE SELECTION SUBSTRATE (forest in -> pruned forest out, feeding the ONE shared composer via _glimpse_dispatch_dom_capture); it grows NO predicates / sort / composer of its own — the moment it did, the pipeline would fork. Dan ACCEPTED this dual walk as legitimate architecture (it mirrors the already-blessed cache_render_spec vs dom_captured_tree source axis), NOT a crack.,
# ]
def build_selected_subtree_from_forest(
    forest: "list[dict[str, Any]] | dict[str, Any] | None",
    member_predicate: "Callable[[dict[str, Any]], bool]",
    *,
    boundary_predicate: "Callable[[dict[str, Any]], bool] | None" = None,
) -> list[dict[str, Any]]:
    """Generic Phase-1 SELECTION over an IN-MEMORY forest (not the cache).

    The forest-walking sibling of :func:`build_selected_subtree_from_cache`.
    Instead of walking the /nodes-export cache down from a ``root_uuid``, it
    walks a PROVIDED nested forest -- the frozen-DOM captured tree the dock
    pinned at freeze (each node ``{id/uuid, name, note, children:[...]}``) --
    ADMITS a node iff ``member_predicate(node)`` is True, and returns the
    minimal connective forest (every admitted node + all its ancestors up to
    each top-level root, child order preserved).

    Output shape is IDENTICAL to the cache primitive: a LIST of nested
    ``{id,name,note,children:[...]}`` roots (a forest, so a multi-root capture
    is preserved). An empty selection yields ``[]``.

    ``member_predicate`` is a pure ``(node) -> bool`` over a node dict. The
    SAME singular predicates the cache path uses (``build_tag_predicate`` /
    ``build_search_predicate``) are reused verbatim -- they read ``name`` /
    ``note`` and are substrate-agnostic, so there is no predicate duplication.

    ``boundary_predicate`` (optional STOP-RECURSION traversal policy, default
    None): when a CHILD satisfies it, the child is still visited (admitted iff
    ``member_predicate`` also holds) but the walk does NOT descend into its
    subtree -- the boundary node renders as a leaf. Mirrors the cache
    primitive's boundary semantics exactly (each top-level root is exempt: the
    test is only applied when deciding whether to descend into a CHILD).

    Pure: builds fresh shadow dicts; the input forest is never mutated.
    """
    # Coerce the forest arg into a top-level list of node dicts. Accept either
    # a list (the composer's forest shape) or a single nested root dict.
    if forest is None:
        return []
    if isinstance(forest, dict):
        roots: list[dict[str, Any]] = [forest]
    elif isinstance(forest, list):
        roots = [n for n in forest if isinstance(n, dict)]
    else:
        return []

    def prune(node: dict[str, Any], is_root: bool) -> dict[str, Any] | None:
        """Return a shadow of ``node`` iff it or a descendant is admitted.

        A node is KEPT when it is itself a member OR any kept descendant
        (connective ancestor). A boundary CHILD is admitted-or-dropped on its
        own membership but never descended into (rendered as a leaf).
        """
        admitted = False
        try:
            admitted = bool(member_predicate(node))
        except Exception:
            admitted = False

        kept_children: list[dict[str, Any]] = []
        # STOP-RECURSION boundary: do not descend into a node that is a
        # boundary (unless it is a top-level root, which is exempt -- the test
        # is applied per CHILD exactly like the cache primitive).
        _is_boundary = False
        if boundary_predicate is not None and not is_root:
            try:
                _is_boundary = bool(boundary_predicate(node))
            except Exception:
                _is_boundary = False
        if not _is_boundary:
            for ch in (node.get("children") or []):
                if not isinstance(ch, dict):
                    continue
                kept = prune(ch, is_root=False)
                if kept is not None:
                    kept_children.append(kept)

        if not admitted and not kept_children:
            return None
        return {
            "id": node.get("id") or node.get("uuid") or "",
            "name": node.get("name"),
            "note": node.get("note"),
            "children": kept_children,
        }

    out: list[dict[str, Any]] = []
    for r in roots:
        kept = prune(r, is_root=True)
        if kept is not None:
            out.append(kept)
    return out


# @beacon[
#   id=f8policy@gather-tags-in-forest,
#   slice_labels=nexus--md-preview,
#   kind=ast,
#   comment=SCOPED LIGHTNING ROD (ap:LRSCOPE) gather-over-forest — the in-memory-forest sibling of gather_tags_in_subtree. Unions every REAL '#'-stripped tag (STRICT recognizer, same as the cache gather) found in a PROVIDED captured-DOM forest, honoring match_scope (name-only vs name+note). Returns the sorted, de-duped '#'-less list. Pure/read-only; used by the DOM-frozen Scoped gather mode so a gather->render_tags handoff surveys the SAME on-screen forest the subsequent render filters (not the whole cache root).,
# ]
def gather_tags_in_forest(
    forest: "list[dict[str, Any]] | dict[str, Any] | None",
    *,
    match_scope: str = "name_and_note",
) -> list[str]:
    """Union of every REAL '#'-stripped tag in an IN-MEMORY forest.

    The forest sibling of :func:`gather_tags_in_subtree`. Walks a provided
    captured-DOM forest (not the cache) and unions the tags found by the STRICT
    :func:`extract_strict_tags_from_text` recognizer over each node's scoped
    field(s).     ``match_scope`` matches the render modes: ``"name"`` harvests the NAME's
    TRAILING tag region only (via the core splitter; a mid-title prose ``#tag``
    or a note-only ``#tag`` is not gathered), ``"name_and_note"`` (default)
    harvests name AND note with the strict recognizer. Returns
    the SORTED, de-duped '#'-stripped tag list (``[]`` for an empty/None
    forest). Pure / read-only.
    """
    if forest is None:
        return []
    if isinstance(forest, dict):
        roots: list[dict[str, Any]] = [forest]
    elif isinstance(forest, list):
        roots = [n for n in forest if isinstance(n, dict)]
    else:
        return []

    _name_only = match_scope == "name"
    found: set[str] = set()

    def _scoped_tags(node: dict[str, Any]) -> list[str]:
        if _name_only:
            return _core_trailing_name_tags(node.get("name"))
        out: list[str] = []
        for _f in ("name", "note"):
            for t in extract_strict_tags_from_text(node.get(_f)):
                if t:
                    out.append(t)
        return out

    def walk(node: dict[str, Any]) -> None:
        for t in _scoped_tags(node):
            found.add(t)
        for ch in (node.get("children") or []):
            if isinstance(ch, dict):
                walk(ch)

    for r in roots:
        walk(r)
    return sorted(found)


# ===========================================================================
# RENDER-SYMBOLS SELECTION HELPERS (dynamic tag-free beacon slices)
# Pure, cache-only ancestor-walk primitives shared by the Lightning Rod
# 'render_symbols' mode: LCA of the passed root(s), the descendant-of-any-root
# scope gate, and the absolute global-suppression ancestor test. All three are
# parent_id walks over the /nodes-export cache dict; no I/O, no mutation.
# ===========================================================================


def _ancestor_chain_ids(cache_by_id: dict[str, dict[str, Any]], node_id: str) -> list[str]:
    """Root-first ancestor id chain INCLUSIVE of ``node_id`` (``[]`` if absent).

    Walks ``parent_id`` upward from ``node_id`` collecting ids, then reverses so
    the result is ordered root-first (index 0 = topmost ancestor in the cache,
    last element = ``node_id`` itself). Cycle-guarded and hop-capped (4096) for
    a malformed cache. Returns ``[]`` when ``node_id`` is not in the cache.
    """
    if node_id not in cache_by_id:
        return []
    chain_leaf_first: list[str] = []
    seen: set[str] = set()
    cur: str | None = node_id
    guard = 0
    while cur and cur not in seen and guard < 4096:
        seen.add(cur)
        chain_leaf_first.append(cur)
        node = cache_by_id.get(cur)
        cur = str(node.get("parent_id")) if node and node.get("parent_id") is not None else None
        guard += 1
    chain_leaf_first.reverse()
    return chain_leaf_first


def is_descendant_of_any_root(
    cache_by_id: dict[str, dict[str, Any]],
    node_id: str,
    root_uuids: Iterable[str],
) -> bool:
    """True iff ``node_id`` is at-or-under any of ``root_uuids`` in the cache.

    Equality counts (a node that IS a passed root is trivially in scope). Pure
    ancestor walk; returns False when ``node_id`` is absent from the cache or
    ``root_uuids`` is empty.
    """
    roots = {str(r) for r in root_uuids if str(r)}
    if not roots:
        return False
    chain = set(_ancestor_chain_ids(cache_by_id, node_id))
    return bool(chain & roots)


# @beacon[
#   id=f8policy@compute-lca-from-cache,
#   slice_labels=slices,
#   kind=ast,
#   comment=render-symbols CONNECTIVE ROOT: compute the lowest common ancestor of a set of root_uuids by intersecting their root-first ancestor chains and taking the DEEPEST (last) shared id. For a SINGLE root the LCA is that root itself (degenerate -- 'begins at the root_uuid'). Returns None if the roots share no common ancestor (disjoint trees) or any is uncached. This is the connective spine fed to build_selected_subtree_from_cache; the descendant-of-any-root gate (not the LCA) is the real membership filter, so a wider LCA never widens what actually renders.,
# ]
def compute_lca_from_cache(
    cache_by_id: dict[str, dict[str, Any]],
    root_uuids: Iterable[str],
) -> str | None:
    """Lowest common ancestor of ``root_uuids`` (deepest shared ancestor id).

    Each root's root-first ancestor chain (via :func:`_ancestor_chain_ids`,
    inclusive of the root itself) is computed; the LCA is the DEEPEST id common
    to every chain. For one root, that is the root itself. Returns ``None`` when
    any root is uncached or the roots are in disjoint trees (no shared
    ancestor) -- the caller then reports a scope error rather than guessing.
    """
    roots = [str(r) for r in root_uuids if str(r)]
    if not roots:
        return None
    chains: list[list[str]] = []
    for r in roots:
        chain = _ancestor_chain_ids(cache_by_id, r)
        if not chain:
            return None  # an uncached root cannot participate in an LCA
        chains.append(chain)
    # Walk the shared prefix of all root-first chains; the LCA is the last id
    # that is identical across every chain at the same depth.
    lca: str | None = None
    for depth in range(min(len(c) for c in chains)):
        ids_at_depth = {c[depth] for c in chains}
        if len(ids_at_depth) != 1:
            break
        lca = chains[0][depth]
    return lca


# @beacon[
#   id=f8policy@global-never-name-match,
#   slice_labels=slices,
#   kind=ast,
#   comment=GLOBAL-NEVER-EXPAND doctrine (Dan, 2026-07, post-IMPPTX audit; tightened 2026-10 by the 084 ruling): the ONE shared stem-normalizer + NAME matcher for the f8_global_never_expand_tags operand. DOCTRINE: global-never suppression reads ONLY the name's TRAILING tag region, under EVERY scope -- it is an ABSOLUTE STRUCTURAL control ('never render this subtree'), so it must be a deliberate, visible TRAILING NAME tag; a tag buried in a NOTE body, or wedged mid-title among prose, never suppresses. (Exact match also gives the user the clean off-switch: rename #wfy-suppress -> #wfy-suppress-old to release while keeping the stem available to re-apply.) normalize_global_never_stems ('#'-optional, whitespace-trimmed, empties dropped) + name_has_global_never_tag (EXACT tag==stem, NOT family/prefix, via _core_trailing_name_tags) are consumed by EVERY reader of the operand so the config field means ONE thing across the whole system: Phase-2 classify_node_tags (has_global_never_expand), render_symbols absolute suppression (node_has_suppress_ancestor), the render_symbols per-item expand sweep (collect_subtree_ids), the merge coverage walks (compute_merge_full_cover / compute_merge_tag_plan), and the HYDSUP Phase-3 hydration seal (apply_expansion_plan_to_subtree). Pure; no I/O.,
# ]
def normalize_global_never_stems(global_never_expand_tags: Iterable[str]) -> set[str]:
    """Normalize ``f8_global_never_expand_tags`` into exact-match stems.

    The ONE normalization for the global-never operand: ``'#'``-optional,
    whitespace-trimmed, empties dropped. Global-never reads the name's
    TRAILING tag region only, under every scope, by doctrine (an absolute
    structural control must be a deliberate, visible TRAILING NAME tag),
    EXACT-match (``tag == stem``, NOT family/prefix) everywhere in the
    system -- see the beacon above.
    """
    stems = {
        str(s).strip().lstrip("#").strip() for s in (global_never_expand_tags or ())
    }
    stems.discard("")
    return stems


# @beacon[
#   id=auto-beacon@name_has_global_never_tag-m0v7,
#   role=name_has_global_never_tag,
#   slice_labels=slices,
#   kind=ast,
#   comment=THE ONE global-never NAME test: True iff the name's TRAILING tag region (via _core_trailing_name_tags, the core splitter) carries an EXACT-match global-never stem -- never a note body, never a mid-title prose mention, under EVERY match_scope (Dan's 084 D1). Change the reading HERE and every consumer (classify_node_tags, node_has_suppress_ancestor, collect_subtree_ids, the HYDSUP hydration seal, the merge coverage walks, _unpersistable_global_never_leaves) follows; never re-implement the test at a call site.,
# ]
def name_has_global_never_tag(name: Any, stems: "set[str] | frozenset[str]") -> bool:
    """True iff ``name``'s TRAILING tags include an exact-match global-never stem.

    Trailing-region-only by doctrine (084 D1): a global-never tag living
    only in a NOTE body, or wedged mid-title among prose, never suppresses
    -- under EVERY scope. ``stems`` comes from
    :func:`normalize_global_never_stems` (pre-normalized once per call site,
    not per node). Pure; an empty stem set reads as False.
    """
    if not stems:
        return False
    return any(t in stems for t in _core_trailing_name_tags(name))


def node_has_suppress_ancestor(
    cache_by_id: dict[str, dict[str, Any]],
    node_id: str,
    suppress_tags: Iterable[str],
) -> bool:
    """True iff ``node_id`` or any ancestor carries a global-suppression tag.

    ``suppress_tags`` is the live ``f8_global_never_expand_tags`` list (the
    extension-options 'Global never-expand tags', e.g. ``['wfy-suppress']``),
    ``'#'``-optional. For each node on ``node_id``'s root->node chain we read
    its name's TRAILING tag region via :func:`name_has_global_never_tag`
    (the core splitter -- 084: a mid-title or note-only global tag never
    suppresses) and test each for an
    EXACT match against a suppression stem.

    EXACT match (NOT prefix/family) is deliberate and load-bearing: the global
    never-expand tags are exact-tag matches independent of prefixes everywhere
    else in the policy (see Phase 2), so using the family matcher here would
    introduce a semantic seam where the SAME config field means two different
    things. Exact match also gives the user a clean off-switch -- editing
    ``#wfy-suppress`` to ``#wfy-suppress-old`` stops it suppressing while the
    base ``wfy-suppress`` stem stays available to re-apply later.

    Any exact hit anywhere on the chain -> True (the whole subtree is
    suppressed). Pure; returns False on an empty suppress list (a user with NO
    suppress tags configured is NOT an error -- nothing is suppressed) or an
    uncached node.
    """
    stems = normalize_global_never_stems(suppress_tags)
    if not stems:
        return False
    for anc_id in _ancestor_chain_ids(cache_by_id, node_id):
        node = cache_by_id.get(anc_id)
        if not node:
            continue
        if name_has_global_never_tag(node.get("name"), stems):
            return True
    return False


# @beacon[
#   id=f8policy@collect-subtree-ids,
#   slice_labels=slices,
#   kind=ast,
#   comment=render_symbols PER-ITEM EXPAND sweep (ap:EXPSWP; D1 member-set sweep, NOT the rejected Phase-3 plan hijack): collect the id of EVERY DESCENDANT of each id in root_ids into ONE set, so a caller-flagged node's whole cache subtree joins the Stage-1 member set exactly 'as though those descendants had been passed in the list'. The SEED ids themselves are NOT included -- the caller unions with its explicit resolved set, which already holds them. SUPPRESSION GUARD: before descending into a child, its name's TRAILING tag region (name_has_global_never_tag, the core splitter) is EXACT-matched against suppress_tags, mirroring node_has_suppress_ancestor's trailing-region exact-match semantics (NOT family/prefix; 084: a mid-title or note-only global tag never suppresses) -- a matching child is neither included nor descended into, so render_symbols' ABSOLUTE suppression doctrine is preserved at sweep time (the seeds are guaranteed non-suppressed by the upstream resolution gate). Does NOT filter completed nodes -- Phase 1 owns completed exclusion at admit time (the sweep adds them harmlessly; collect() never materializes them). Boundary-free by design: a flagged node's subtree is swept fully, child anchors included (the selection is an explicit agent request; descend_policy stays rejected for render_symbols). Pure, no I/O; sibling of _ancestor_chain_ids / is_descendant_of_any_root / compute_lca_from_cache.,
# ]
def collect_subtree_ids(
    cache_by_id: dict[str, dict[str, Any]],
    root_ids: Iterable[str],
    *,
    suppress_tags: Iterable[str] = (),
) -> set[str]:
    """Collect every DESCENDANT id of each id in ``root_ids`` (seeds excluded).

    The render_symbols per-item ``expand`` sweep (ap:EXPSWP). DFS from each
    seed id; collect every descendant id into ONE set. The seeds themselves
    are NOT in the result -- the caller unions with its explicit resolved-id
    set, which already contains them.

    SUPPRESSION GUARD: before descending into a child, the child's name
    TRAILING tag region (via :func:`name_has_global_never_tag`, the core
    splitter) is exact-matched against
    ``suppress_tags`` (the live ``f8_global_never_expand_tags`` list,
    ``'#'``-optional) -- mirroring :func:`node_has_suppress_ancestor`'s
    trailing-region, EXACT-match semantics (NOT family/prefix; 084: a
    mid-title or note-only global tag never suppresses), so
    ``f8_global_never_expand_tags`` means the SAME thing here as everywhere
    else (no semantic seam). A matching child is NEITHER included NOR
    descended into, so suppressed content never enters the swept member set.
    The seeds are NOT tested (the upstream resolution gate already
    guarantees they are non-suppressed).

    COMPLETED NODES ARE COLLECTED, NOT FILTERED: completed exclusion is a
    Phase-1 admission policy (``exclude_completed`` prunes at admit time),
    so sweeping a completed descendant id into the set is harmless --
    ``build_selected_subtree_from_cache`` never materializes it. Pure, no
    I/O; child order irrelevant (the deliverable is a set).
    """
    stems = normalize_global_never_stems(suppress_tags)

    # children index by parent_id -- the SAME construction Phase 1
    # (build_selected_subtree_from_cache) / Phase 3 use; order is irrelevant
    # here because the deliverable is a SET, not an ordered render tree.
    children_by_parent: dict[str, list[str]] = {}
    for node in cache_by_id.values():
        pid = node.get("parent_id")
        nid = _node_id(node)
        if pid is not None and nid:
            children_by_parent.setdefault(str(pid), []).append(nid)

    collected: set[str] = set()

    def _sweep(nid: str) -> None:
        for cid in children_by_parent.get(nid, []):
            if cid in collected:
                continue
            child = cache_by_id.get(cid)
            if child is None:
                continue
            # SUPPRESSION GUARD (absolute, NAME-only exact match, via the
            # ONE shared doctrine helper): a child carrying a global-never-
            # expand tag is NEITHER included NOR descended into -- the
            # suppressed pocket stays out of the member set by construction.
            if name_has_global_never_tag(child.get("name"), stems):
                continue
            collected.add(cid)
            _sweep(cid)

    for rid in root_ids:
        _sweep(str(rid))
    return collected


def build_tag_predicate(
    requested_prefixes: Iterable[str],
    *,
    match_scope: str = "name_and_note",
) -> "Callable[[dict[str, Any]], bool]":
    """Build a Lightning-Rod tag membership predicate (Option B family match).

    Prefix-FAMILY matching with NO postfix semantics: a requested stem
    ``monitor`` admits ``#monitor``, ``#monitor-dell``, ``#monitor-lg-4k`` (and
    even ``#monitor--deprecated`` -- Lightning Rod does not special-case the
    deprecation/suppress/expand postfixes at all), but NOT ``#monitorx`` (the
    canonical matcher's empty-or-hyphen middle-boundary rule). Reuses the ONE
    shared family matcher ``slice_tag_variant_match`` with ``postfix_words=()``
    so no trailing ``--word`` is ever peeled as a postfix -- keeping the
    behavior purely "does this tag start this family?" with no exclusion logic.

    ``match_scope`` is the SELECTION-stage scope knob (keyword-only), the ONE
    place this predicate decides WHERE to harvest the node's tags:
      * ``"name_and_note"`` (default -- prior behavior) reads tags from NAME +
        NOTE via :func:`extract_node_tags` (Workflowy search semantics), so a
        node tagged only in its note still matches.
      * ``"name"`` reads tags from the NAME (title) ONLY -- specifically the
        name's TRAILING tag region via the core splitter
        (``_core_trailing_name_tags``) -- so neither a mid-title prose ``#tag``
        nor a tag living only in the note body admits the node.
    This is purely a SELECTION concern; it has nothing to do with the
    ``--expand``/``--suppress``/``--deprecated`` postfix OPERATION policy
    (a separate later stage that reads NAME+NOTE via
    ``classify_node_tags``/``extract_node_tags``, independent of this
    SELECTION scope). Any unrecognized value falls safe to the broader
    name+note harvest.
    """
    # Normalize each requested stem: strip surrounding whitespace FIRST, then a
    # single leading '#', then re-strip (so '  #monitor ', '# monitor', and
    # 'monitor' all reduce to 'monitor'). Order matters -- lstrip('#') before
    # the outer strip would leave a leading '#' intact when the token had
    # leading whitespace (' #monitor' -> '#monitor'), breaking the match.
    reqs = [
        _stem
        for _stem in (str(p).strip().lstrip("#").strip() for p in requested_prefixes)
        if _stem
    ]

    def _pred(node: dict[str, Any]) -> bool:
        if not reqs:
            return False
        if match_scope == "name":
            tags = _core_trailing_name_tags(node.get("name"))
        else:
            tags = extract_node_tags(node.get("name"), node.get("note"))
        for t in tags:
            for p in reqs:
                # NOTE: we deliberately IGNORE the returned dict's "postfix"
                # field -- a match is a match regardless of any --word tail.
                if slice_tag_variant_match(t, p, postfix_words=()) is not None:
                    return True
        return False

    return _pred


# @beacon[
#   id=f8policy@gather-tags-in-subtree,
#   slice_labels=slices,
#   kind=ast,
#   comment=LIGHTNING ROD gather-tags: union of all REAL '#'-stripped tags found under root_uuid in the /nodes-export cache, sorted + de-duped. match_scope (SELECTION-stage scope knob) picks WHERE tags are harvested: 'name_and_note' (default) -> name AND note (strict recognizer); 'name' -> the NAME's TRAILING tag region via the core splitter (_core_trailing_name_tags; a mid-title prose '#tag' or a note-only '#tag' is not gathered) -- matched to the two render modes so a gather->render_tags handoff surveys exactly the fields the render will filter on. boundary_predicate (Phase-C descend_policy='stop_at_child_anchors', default None) is a STOP-RECURSION traversal knob: when a descendant satisfies it (a child anchor), that node's OWN tags are still harvested (visible signpost) but its subtree is NOT surveyed -- so gather and a subsequent render_tags see the SAME bounded universe (root exempt). Uses the STRICT extract_strict_tags_from_text recognizer (NOT the generalized Workflowy-search harvester) so it does NOT return junk harvested from '#' glued inside prose/code (').strip()', 'f8-expand),', etc.). Lightning Rod is unmoored/divergent-from-Workflowy, so gather is free to be smarter about what counts as a genuine tag. Pure, no rendering, no pipeline -- the discovery call whose result feeds a follow-up Lightning Rod render_tags.,
# ]
def gather_tags_in_subtree(
    cache_by_id: dict[str, dict[str, Any]],
    root_uuid: str,
    *,
    match_scope: str = "name_and_note",
    boundary_predicate: "Callable[[dict[str, Any]], bool] | None" = None,
) -> list[str] | None:
    """Union of every REAL '#'-stripped tag under ``root_uuid`` (name OR note).

    The Lightning Rod GATHER mode. Walks every descendant of ``root_uuid`` in
    the cache (the root itself included) and unions the tags found by the
    STRICT :func:`extract_strict_tags_from_text` recognizer over each node's
    fields (see ``match_scope``). Returns the SORTED, de-duped '#'-stripped tag
    list, or None if ``root_uuid`` is absent from the cache.

    ``match_scope`` is the SELECTION-stage scope knob (keyword-only), matching
    the two render modes so a gather->render_tags handoff surveys exactly the
    fields the subsequent render will filter on:
      * ``"name_and_note"`` (default -- prior behavior) harvests tags from each
        node's name AND note.
      * ``"name"`` harvests tags from the NAME's TRAILING tag region ONLY,
        via the core splitter (``_core_trailing_name_tags``): a mid-title prose
        ``#tag`` or a tag living only in a note body is not gathered (name-only
        is a semantic tag search -- F1 names gather explicitly).
    Any unrecognized value falls safe to the broader name+note harvest.

    Under ``"name_and_note"`` uses the STRICT recognizer -- NOT the generalized,
    Workflowy-search-parity ``extract_node_tags`` -- ON PURPOSE: gather feeds a
    HUMAN-FACING tag list, and the generalized harvester returns ugly false
    positives on prose/code notes (a ``#`` glued inside ``lstrip("#tail")``, a
    tag with a trailing ``),``). Lightning Rod is already an unmoored,
    divergent-from-Workflowy surface, so it is free to recognize only genuine
    tags here. (Name-only instead reads the trailing region via the core
    splitter, which is stricter still.) (Slice MEMBERSHIP still uses the
    generalized harvester elsewhere -- do not confuse the two.) Pure /
    read-only: no rendering, no pipeline, no I/O.
    """
    if root_uuid not in cache_by_id:
        return None

    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for node in cache_by_id.values():
        pid = node.get("parent_id")
        if pid is not None:
            children_by_parent.setdefault(str(pid), []).append(node)

    found: set[str] = set()

    # SELECTION scope (decided ONCE here, not per-node, so the walk stays tight):
    #   * 'name'           -> the NAME's TRAILING tag region via the core
    #     splitter (_core_trailing_name_tags). Name-only is a SEMANTIC tag search
    #     (F1 names gather explicitly), so a mid-title prose '#tag' or a tag in
    #     the note body is NOT gathered -- the same authority the mutation core
    #     uses. The strict recognizer is NOT used here: name-only read the
    #     trailing region, full stop.
    #   * 'name_and_note'  -> today's STRICT recognizer over name AND note
    #     (unchanged; the recognizer itself is untouched).
    _name_only = match_scope == "name"
    _fields = ("name", "note")

    def _scoped_tags(node: dict[str, Any]) -> list[str]:
        if _name_only:
            return _core_trailing_name_tags(node.get("name"))
        found_here: list[str] = []
        for _f in _fields:
            for t in extract_strict_tags_from_text(node.get(_f)):
                if t:
                    found_here.append(t)
        return found_here

    def walk(nid: str) -> None:
        node = cache_by_id.get(nid)
        if node is None:
            return
        for t in _scoped_tags(node):
            found.add(t)
        # STOP-RECURSION boundary (Phase-C descend_policy). The boundary node
        # itself is harvested as a visible signpost, but its descendants are not
        # surveyed; root is exempt so pointing at an anchor still surveys its
        # immediate subtree until the next child-anchor boundary.
        if nid != root_uuid and boundary_predicate is not None and boundary_predicate(node):
            return
        for ch in children_by_parent.get(nid, []):
            cid = _node_id(ch)
            if cid:
                walk(cid)

    walk(root_uuid)
    return sorted(found)


# @beacon[
#   id=f8policy@render-tags-outline,
#   slice_labels=slices,
#   kind=ast,
#   comment=LIGHTNING ROD gather SERIALIZER -- the tag-domain peer of the node-tree composer. Turns a flat, sorted tag list (from gather_tags_in_subtree) into a prefix-grouped, hyphen-segment-nested RAW TEXT outline (4 spaces per level, alphabetical within a level). This is the intentional serialization seam for gather: gather produces a TAG SET (not a node tree), so its correct serializer is THIS pure function, NOT the node-tree render tail render_stage2_filter_sort (which would need a fake tree). The shared universal contract is 'one final rendered string', re-unified at the delivery boundary, not at the tree renderer. HYPHEN RULE (Dan): a run of >=1 hyphens collapses to a SINGLE separator, then split -- so double-hyphen postfixes (--deprecated/--suppress/--expand) are NOT special-cased and just become an ordinary trailing segment. SYNTHESIZED PARENTS: an intermediate segment path that is not itself a literal gathered tag still gets an outline header line for readability; those synthetic headers appear ONLY in this text, never in the caller's literal `tags` array. Pure / no I/O -- the tag-set analog of _build_markdown_preview_from_tree.,
# ]
def render_tags_outline(tags: "Iterable[str] | None") -> str:
    """Render a flat tag list as a prefix-grouped, nested raw-text outline.

    The Lightning Rod GATHER serializer -- the tag-domain sibling of the
    node-tree composer. Given the sorted, de-duped tag list from
    :func:`gather_tags_in_subtree`, produce a human-scannable outline where
    each tag is nested under its prefix family: one indent level (4 spaces)
    per hyphen-delimited segment, alphabetical within each level.

    Example::

        #comm
            #comm-jim
                #comm-jim-phone
                #comm-jim-teams
            #comm-rob
        #nexus
            #nexus-md-preview

    TWO DECOUPLED CONCERNS (Dan, the hyphen-fix):

    (1) FAMILY / NESTING KEY -- structure only. A tag's nesting position is
        decided by a NORMALIZED key: collapse any run of hyphens to a single
        separator, DROP a recognized trailing postfix segment
        (``--deprecated`` / ``--suppress`` / ``--expand``), then split into
        segments. So ``comm-jim--deprecated`` and ``comm-jim`` share the key
        ``[comm, jim]`` (the postfix contributes NO nesting level), and
        ``comm-jim-teams`` (key ``[comm, jim, teams]``) nests one level
        DEEPER than either. A ``--``-variant and a ``-``-variant of the same
        intermediate segment collapse into the same branch (an accepted edge).

    (2) DISPLAY STRING -- the emitted line shows the tag VERBATIM, exactly as
        it exists (original hyphens + postfix intact, e.g.
        ``#comm-jim-teams--deprecated``). Display NEVER uses the normalized
        key. When two literal tags share one key (e.g. ``comm-jim`` AND
        ``comm-jim--deprecated``), BOTH render as sibling lines at that node.

    NODE EMISSION RULE: a trie node emits row(s) iff it carries >=1 LITERAL
    tag OR it is a real BRANCH point (>=2 children). Otherwise it is a
    transparent single-child PASS-THROUGH and is skipped, so a chain of
    segment-only ancestors with nothing to distinguish does NOT synthesize
    empty parents -- the deepest tag simply roots at the nearest EMITTING
    ancestor (or the top). Indent depth counts only EMITTED ancestors.

    SYNTHETIC LABEL: a branch node (>=2 children) that has NO literal tag has
    no original to show, so it emits the reconstructed collapsed-key label
    (``#comm``). This is the ONLY line that is not a verbatim original, and it
    is unavoidable (no original exists at that key).

    DISPLAY '#' PREFIX: every emitted line carries a leading ``#``. Display-
    only: the caller's ``tags`` array remains canonical; matching is untouched.

    Pure / no I/O. Returns "" for an empty/None input.
    """
    # Recognized trailing postfix words (default set: deprecated/suppress/
    # expand). Gather is an unmoored display surface with no live config here,
    # so the defaults are the right recognizer; a custom-postfix vault is a
    # rare edge that at worst shows one extra nesting level, never wrong data.
    _postfix_words = set(_recognized_postfix_words())

    # NORMALIZED KEY (structure only): collapse hyphen runs, split, then drop a
    # single recognized trailing postfix segment (a postfix is only legal at
    # the end). The key decides nesting; it is NEVER displayed.
    def _key_segments(tag: str) -> list[str]:
        collapsed = re.sub(r"-+", "-", tag)
        segs = [seg for seg in collapsed.split("-") if seg]
        if len(segs) >= 2 and segs[-1] in _postfix_words:
            segs = segs[:-1]
        return segs

    # Build a segment trie keyed by the NORMALIZED key. Each node stores its
    # children AND the SET of VERBATIM originals whose key lands here (usually
    # one; two when a plain + postfixed variant share the key).
    class _Node:
        __slots__ = ("children", "originals")

        def __init__(self) -> None:
            self.children: dict[str, _Node] = {}
            self.originals: set[str] = set()

    root_node = _Node()
    for raw in tags or []:
        raw_s = str(raw)
        segs = _key_segments(raw_s)
        if not segs:
            continue
        node = root_node
        for seg in segs:
            node = node.children.setdefault(seg, _Node())
        node.originals.add(raw_s)

    # DFS emit with PASS-THROUGH COLLAPSE. A node is EMITTING iff it has a
    # literal original OR >=2 children; a single-child node with no original is
    # transparent (its child re-roots at this same depth). Depth counts only
    # emitted ancestors.
    lines: list[str] = []

    def _emit(node: "_Node", key_path: list[str], depth: int) -> None:
        for seg in sorted(node.children.keys()):
            child = node.children[seg]
            child_path = key_path + [seg]
            is_branch = len(child.children) >= 2
            has_literal = bool(child.originals)
            if has_literal or is_branch:
                if has_literal:
                    # Verbatim originals (both, if a plain + postfixed variant
                    # share this key), as sibling lines.
                    for original in sorted(child.originals):
                        lines.append(("    " * depth) + "#" + original.lstrip("#"))
                else:
                    # Synthetic branch header: no original exists at this key,
                    # so show the reconstructed collapsed-key label.
                    lines.append(("    " * depth) + "#" + "-".join(child_path))
                _emit(child, child_path, depth + 1)
            else:
                # Transparent single-child pass-through: skip this node, do NOT
                # consume an indent level -- the descendant roots here.
                _emit(child, child_path, depth)

    _emit(root_node, [], 0)
    return "\n".join(lines)


# @beacon[
#   id=f8policy@phase1-build-subtree-wrapper,
#   slice_labels=slices,
#   kind=ast,
#   comment=SLICE-GROUP wrapper over the generic build_selected_subtree_from_cache: it supplies the slice-group admit predicate so the generic primitive does the tree walk. member_all=True -> lambda node: True (the raw-dump: every descendant is a member); otherwise -> the is_slice_member closure (per-prefix membership, deprecated-excluding, NAME-only or NAME+NOTE per match_scope). exclude_completed, boundary_predicate and max_depth (ap:EPR86W part a: the raw-dump recursive=false depth cap; a membership projection is unbounded by construction) thread straight through to the generic primitive's traversal policies. Callers: the slice-group GLIMPSE, the Apply-slice modal, and (via member_all) the raw-dump root GLIMPSE.,
# ]
def build_slice_subtree_from_cache(
    cache_by_id: dict[str, dict[str, Any]],
    root_uuid: str,
    active_prefixes: Iterable[str],
    *,
    deprecated_postfix: str = DEFAULT_DEPRECATED_POSTFIX,
    auto_expand_postfix: str = DEFAULT_AUTO_EXPAND_POSTFIX,
    suppress_postfix: str = DEFAULT_SUPPRESS_POSTFIX,
    global_never_expand_tags: Iterable[str] = (),
    member_all: bool = False,
    exclude_completed: bool = False,
    match_scope: str = "name_and_note",
    boundary_predicate: "Callable[[dict[str, Any]], bool] | None" = None,
    max_depth: int | None = None,
) -> dict[str, Any] | None:
    """Build the minimal SLICE subtree from the cache (Phase 1, no DOM).

    Wrapper over :func:`build_selected_subtree_from_cache` (the generic Stage-1
    SELECTION primitive) that supplies the slice-group admit predicate:

      1. Collect every descendant of ``root_uuid`` that is a MEMBERSHIP hit
         under the per-prefix rule (``is_slice_member``): ∃ active prefix P
         matched AND P NOT deprecated on that node.
      2. Build the minimal connective subtree: every hit + all ancestors up to
         (and including) ``root_uuid``, with children ordered by ``priority``.

    Returns a nested ``{id,name,note,children:[...]}`` root, or None if
    ``root_uuid`` is absent from the cache.

    ``member_all`` (RAW-DUMP mode, default False): when True, EVERY descendant
    of ``root_uuid`` is a member unconditionally -- the COMPLETE subtree, no
    membership filtering. ``active_prefixes`` is irrelevant (and may be empty)
    in this mode, and the caller runs no Phase 2/3 (a raw dump has no
    slice-presentation policy).

    ``exclude_completed`` (default False) threads straight through to the
    generic primitive's traversal policy -- a completed ancestor hides its
    entire subtree at ADMIT time; the root is exempt by construction.

    ``match_scope`` (SELECTION-stage scope knob, default ``"name_and_note"``)
    threads into the ``is_slice_member`` predicate: ``"name"`` restricts
    membership to the node TITLE only, ``"name_and_note"`` matches name+note.
    Irrelevant in ``member_all`` raw-dump mode (every node is a member
    regardless).

    ``boundary_predicate`` (default None) threads straight through to the
    generic primitive: when a CHILD satisfies it, the walk emits that child as
    a LEAF and does NOT descend into its subtree (root exempt). This is how a
    raw-dump (``member_all``) or slice projection stops at child anchors -- see
    :func:`build_anchor_boundary_predicate`.

    ``max_depth`` (default None) threads straight through to the generic
    primitive's THIRD traversal policy (ap:EPR86W part a): a depth cap where
    the root is depth 0, so ``1`` renders the root + its direct children only
    (the spec's ``recursive=False`` maps here). Only the raw-dump surfaces
    (``member_all=True``) thread a cap today; a membership projection is
    unbounded by construction and refuses ``recursive=False`` loudly at the
    spec normalizer.
    """
    active_prefixes = list(active_prefixes)
    if member_all:
        # RAW-DUMP short-circuit: every node is a member.
        member_predicate = lambda node: True  # noqa: E731
    else:
        # Per-prefix membership (the single shared predicate). match_scope
        # decides NAME-only vs NAME+NOTE harvest inside is_slice_member.
        def member_predicate(node: dict[str, Any]) -> bool:
            return is_slice_member(
                node.get("name"),
                active_prefixes,
                note=node.get("note"),
                deprecated_postfix=deprecated_postfix,
                match_scope=match_scope,
            )
    return build_selected_subtree_from_cache(
        cache_by_id,
        root_uuid,
        member_predicate,
        exclude_completed=exclude_completed,
        boundary_predicate=boundary_predicate,
        max_depth=max_depth,
    )


# ===========================================================================
# PHASE 0 — strict DOM-subtree validator (fail-closed)
# ===========================================================================

def _flatten(root: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, str | None]]:
    """Pre-order DFS flatten -> (ordered nodes, by_id, parent_of).

    Mirrors the shape of api_client_nexus._flatten_modal_tree (kept local so
    this module has no class dependency). ``by_id`` nodes carry a synthesized
    ``parent_id``; ``parent_of`` maps uuid -> parent uuid (None at root).

    EMPTY-ID ROOT IS TRANSPARENT (the Workflowy "Home" case): under an active
    tag search Workflowy roots the rendered project at the synthetic top-level
    "Home" node, whose ``projectid`` is EMPTY and which is ABSENT from the
    /nodes-export cache. F8 may therefore hand us a root (or, in principle, an
    interior node) with no id. Such a node is NOT a real operable node, so we
    DO NOT add it to ordered/by_id/parent_of -- but we MUST still descend into
    its children, attaching them to the empty node's OWN parent (None at the
    true top). This makes the empty Home root transparent: its children (the
    real top-level nodes, whose cache ``parent_id`` is None) are walked
    normally instead of the whole walk aborting (which previously produced a
    spurious ``empty_tree`` / mismatch and a wrong root). A non-empty-id node
    is recorded as before.
    """
    ordered: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    parent_of: dict[str, str | None] = {}

    def walk(node: dict[str, Any], parent: str | None) -> None:
        nid = _node_id(node)
        if not nid:
            # Transparent synthetic container (e.g. Home): skip the node
            # itself but keep walking its children under the SAME parent.
            for ch in _children(node):
                walk(ch, parent)
            return
        ordered.append(node)
        by_id[nid] = node
        parent_of[nid] = parent
        for ch in _children(node):
            walk(ch, nid)

    walk(root, None)
    return ordered, by_id, parent_of


# @beacon[
#   id=f8policy@phase0-validate,
#   slice_labels=slices,
#   kind=ast,
#   comment=PHASE 0 (DOM path) — UUID-MEMBERSHIP-ONLY validation that the browser's visible subtree is known to the /nodes-export cache: every DOM uuid must EXIST in cache. Deliberately NO parent->child edge (parent_id) comparison and NO node-content (name/note) comparison — exactly the proven docked-modal staleness contract (_modal_staleness), which dropped both as unwinnable/overzealous (parent_id boundary+normalization mismatches at the zoom root / virtual nodes produced constant false 'cache_stale'). Fail-closed only on a genuinely-missing UUID (F8 must not act on a node the server doesn't know); a present UUID is sufficient. The inverse direction (cache nodes not in the DOM) is NOT staleness (the DOM is visible-only).,
# ]
def validate_subtree_against_cache(
    root: dict[str, Any],
    cache_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Validate a DOM subtree against the cache by UUID membership ONLY.

    Returns ``{"ok": True}`` when valid, else
    ``{"ok": False, "error": <code>, "message": <str>, "details": [...]}``.

    This mirrors the docked Markdown modal's PROVEN staleness contract
    (``_modal_staleness`` in api_client_nexus): the ONLY check is that every
    UUID the browser currently shows EXISTS in the /nodes-export cache. There
    is deliberately:

      * NO node-content (NAME/NOTE) comparison — removed as unwinnable.
      * NO parent->child edge (``parent_id``) comparison — the earlier strict
        edge check was OVERZEALOUS: ``parent_id`` representation/normalization
        differs at boundaries (the zoom root, ``virtual_`` mirror nodes, the
        root sentinel), so it produced constant FALSE ``visible_tree_cache_
        mismatch`` errors even on a freshly-refreshed cache. The modal hit the
        same wall and dropped the edge check; F8 follows suit.

    A UUID that is genuinely ABSENT from the cache is still a hard error
    (fail-closed): F8 mutates the tree, so acting on a node the server has
    never seen is unsafe — the caller refreshes the cache and retries. But a
    node that is PRESENT (regardless of where the cache thinks its parent is)
    is accepted. The inverse direction (cache nodes not shown in the DOM) is
    NOT staleness, because the DOM is visible-only and the cache legitimately
    holds many hidden/collapsed descendants.
    """
    if cache_by_id is None or not cache_by_id:
        return {
            "ok": False,
            "error": "cache_unavailable",
            "message": "Nodes cache is unavailable; cannot validate the visible tree. Refresh the nodes cache and retry.",
            "details": [],
        }

    ordered, _by_id, _parent_of = _flatten(root)
    if not ordered:
        return {"ok": False, "error": "empty_tree", "message": "Visible tree is empty or malformed.", "details": []}

    # UUID-membership only: every visible UUID must exist in the cache. (No
    # parent_id edge comparison — see the docstring / beacon for why the strict
    # edge check was removed as overzealous, matching the modal's contract.)
    missing: list[str] = [_node_id(node) for node in ordered if _node_id(node) not in cache_by_id]

    if missing:
        return {
            "ok": False,
            "error": "visible_tree_cache_mismatch",
            "message": (
                "The visible Workflowy tree contains node(s) the server nodes cache "
                "has not ingested yet. Refresh the nodes cache and retry."
            ),
            "details": {"missing": missing},
        }

    return {"ok": True}


# @beacon[
#   id=f8policy@diagnose-subtree-edges,
#   slice_labels=slices,
#   kind=ast,
#   comment=DIAGNOSTIC ONLY (not a gate) — compare the visible DOM subtree's parent->child edges against the /nodes-export cache parent_id and return RICH per-row mismatch detail (node id+name, dom_parent id+name, cache_parent id+name). Purpose: empirically find WHY the old strict edge check tripped (search-mode serialization vs canonical tree) rather than theorize. Returns {missing:[...], edge_mismatches:[...], checked:int, root_id, root_name}. Never raises; never fails the plan.,
# ]
def diagnose_subtree_edges(
    root: dict[str, Any],
    cache_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return rich DOM-vs-cache edge diagnostics (informational only).

    For every node in the visible subtree EXCEPT the root, compare the DOM-
    derived parent (the node's parent in the serialized ``{root,children}``
    tree) against the cache's ``parent_id`` for that node. Report every
    divergence with human-readable names on BOTH sides so we can see exactly
    which edge disagrees and what the cache thinks the true parent is.

    This is a debugging instrument; it does NOT gate the F8 plan. The plan is
    gated only by ``validate_subtree_against_cache`` (UUID-membership).
    """
    out: dict[str, Any] = {
        "checked": 0,
        "missing": [],
        "edge_mismatches": [],
        "root_id": None,
        "root_name": None,
    }
    if not isinstance(cache_by_id, dict) or not cache_by_id:
        out["error"] = "cache_unavailable"
        return out

    ordered, _by_id, parent_of = _flatten(root)
    if not ordered:
        out["error"] = "empty_tree"
        return out

    root_id = _node_id(root)
    out["root_id"] = root_id
    out["root_name"] = (root.get("name") if isinstance(root, dict) else None)

    def _cache_name(nid: str | None) -> str | None:
        if nid is None:
            return None
        node = cache_by_id.get(nid)
        return node.get("name") if isinstance(node, dict) else None

    # DOM names (from the serialized tree) for readable rows.
    dom_name_by_id: dict[str, Any] = {}
    for node in ordered:
        nid = _node_id(node)
        if nid:
            dom_name_by_id[nid] = node.get("name")

    for node in ordered:
        nid = _node_id(node)
        if nid not in cache_by_id:
            out["missing"].append({"node": nid, "node_name": node.get("name")})
            continue
        dom_parent = parent_of.get(nid)
        if dom_parent is None:
            continue  # the subtree root: true parent is above the slice; not checked
        out["checked"] += 1
        cache_parent = cache_by_id[nid].get("parent_id")
        cache_parent = str(cache_parent) if cache_parent is not None else None
        if dom_parent != cache_parent:
            out["edge_mismatches"].append({
                "node": nid,
                "node_name": node.get("name"),
                "dom_parent": dom_parent,
                "dom_parent_name": dom_name_by_id.get(dom_parent),
                "cache_parent": cache_parent,
                "cache_parent_name": _cache_name(cache_parent),
            })

    return out


# ===========================================================================
# PHASE 2 — expansion-plan builder (suppress / deprecated / expand)
# ===========================================================================

# @beacon[
#   id=f8policy@phase2-plan,
#   slice_labels=slices,
#   kind=ast,
#   comment=PHASE 2 (the heart) — given a VALID Phase-1 subtree + active slice config, compute the expansion plan {collapse_suppressed, collapse_deprecated, expand}. Annotates each node, applies the "don't collapse a deprecated node through a live in-slice descendant" rescue rule, normalizes each set topmost-only, and resolves cross-set conflicts via the ASYMMETRIC merge: a contraction dominates a nested expansion, but an expansion does NOT dominate a nested contraction -- so a suppress/deprecated pocket can survive inside an --expand region. BOTH Phase 3 (server) and the JS client executeF8ExpansionPlan execute the plan EXPAND FIRST, THEN collapse_suppressed, THEN collapse_deprecated -- expanding first so a nested contraction is not overwritten by a later re-hydration. Also emits the Tier-2 pending_* live-GUI-sync metadata + the deprecated-body-suppression body_suppressed_ids set (surviving --deprecated nodes render header-only). match_scope (default name_and_note) threads the SELECTION-stage scope into THREE questions: the rescue's liveness membership (compute_live's is_slice_member; post-Sol-audit Finding 2), the promotion pass's ACTIVE-self check (node_is_live_self title-harvests under 'name'), AND -- 084 ruling -- the --expand/--suppress/--deprecated OPERATION classification itself (annotate passes it into classify_node_tags: under 'name' a family postfix counts only in the name's TRAILING tag region; under 'name_and_note' it also counts in the note). The global-never tag is TRAILING-only under every scope (name_has_global_never_tag).,
# ]
def build_expansion_plan(
    root: dict[str, Any],
    active_prefixes: Iterable[str],
    *,
    auto_expand_postfix: str = DEFAULT_AUTO_EXPAND_POSTFIX,
    suppress_postfix: str = DEFAULT_SUPPRESS_POSTFIX,
    deprecated_postfix: str = DEFAULT_DEPRECATED_POSTFIX,
    global_never_expand_tags: Iterable[str] = (),
    match_scope: str = "name_and_note",
) -> dict[str, Any]:
    """Compute the F8 expansion plan over a valid subtree (Phase 2).

    Returns::

        {
          "collapse_suppressed":  [uuid, ...],   # topmost-only
          "collapse_deprecated":  [uuid, ...],   # topmost-only, live-rescued
          "expand":               [uuid, ...],   # topmost-only, conflict-pruned
          "warnings":             [str, ...],
        }

    See the module v1-SEMANTICS block for the precise rules.
    """
    active_prefixes = list(active_prefixes)
    warnings: list[str] = []

    # --- annotate every node ---
    flags_by_id: dict[str, dict[str, bool]] = {}
    node_by_id: dict[str, dict[str, Any]] = {}
    parent_of: dict[str, str | None] = {}
    order: list[str] = []

    def annotate(node: dict[str, Any], parent: str | None) -> None:
        nid = _node_id(node)
        if not nid:
            # Transparent synthetic container (e.g. the Workflowy "Home" root
            # under an active search): skip it but still annotate its children
            # under the SAME parent, mirroring _flatten's empty-id handling so
            # Phase 2 plans the real top-level nodes instead of aborting.
            for ch in _children(node):
                annotate(ch, parent)
            return
        order.append(nid)
        parent_of[nid] = parent
        node_by_id[nid] = node
        flags_by_id[nid] = classify_node_tags(
            node.get("name"),
            active_prefixes,
            note=node.get("note"),
            auto_expand_postfix=auto_expand_postfix,
            suppress_postfix=suppress_postfix,
            deprecated_postfix=deprecated_postfix,
            global_never_expand_tags=global_never_expand_tags,
            match_scope=match_scope,
        )
        for ch in _children(node):
            annotate(ch, nid)

    annotate(root, None)

    # --- bottom-up: does this subtree contain a LIVE slice MEMBER? ---
    # "live" uses the SAME per-prefix membership predicate as Phase 1
    # (is_slice_member): a node is live iff some active prefix is matched and
    # NOT deprecated on it. This keeps the deprecated "don't collapse through
    # live content" rescue consistent with membership (a node deprecated on
    # one prefix but live on another counts as live and must not be hidden).
    #
    # Liveness for the deprecated-RESCUE rule EXCLUDES suppressed-subtree
    # content: a node that is a slice member but ALSO carries a suppress (or
    # global-never-expand) tag is NOT live, and neither is anything under a
    # suppression root (we stop descending there). Otherwise a deprecated
    # ANCESTOR above a suppressed node would be wrongly rescued from collapse
    # -- kept open only by content that is itself going to be hidden. The
    # rescue's intent is "a node that is a slice member AND is neither
    # suppressed NOR under a suppression root"; the suppression-aware pass
    # below computes exactly that. A useful consequence: when the ONLY thing
    # beneath a deprecated node is suppressed content, the subtree is not live,
    # so the deprecated node collapses and the upward prune walks cleanly
    # THROUGH the now-non-live suppression node. This `contains_live` map is
    # consumed ONLY by the deprecated-rescue predicate
    # (descendants_contain_live); it does NOT affect Phase 1 membership or the
    # suppress/expand candidate collection.
    contains_live: dict[str, bool] = {}

    def _is_suppressed_node(node: dict[str, Any]) -> bool:
        f = flags_by_id.get(_node_id(node), {})
        return bool(f.get("has_suppress") or f.get("has_global_never_expand"))

    def compute_live(node: dict[str, Any]) -> bool:
        nid = _node_id(node)
        # A suppression root (suppress / global-never-expand) and its ENTIRE
        # subtree are not "live for rescue": the subtree will be collapsed, so
        # nothing in it can justify keeping a deprecated ancestor open. Stop
        # descending -- mark the whole subtree non-live.
        if _is_suppressed_node(node):
            if nid:
                contains_live[nid] = False
            # Still record descendants as non-live for completeness (so a
            # later lookup on a suppressed descendant is deterministic).
            for ch in _children(node):
                _mark_subtree_not_live(ch)
            return False
        # POST-SOL-AUDIT FIX (Finding 2): the rescue's liveness is a
        # MEMBERSHIP question, so it must honor the SAME match_scope Phase 1
        # used -- a title-only slice must not have a deprecated branch rescued
        # by a note-only tag. match_scope ALSO governs the annotation above
        # (084): classify_node_tags reads the family postfixes from the
        # trailing tag region under 'name'; global-never is trailing-only
        # under every scope.
        self_live = is_slice_member(
            node.get("name"),
            active_prefixes,
            note=node.get("note"),
            deprecated_postfix=deprecated_postfix,
            match_scope=match_scope,
        )
        any_child = False
        for ch in _children(node):
            if compute_live(ch):
                any_child = True
        result = self_live or any_child
        if nid:
            contains_live[nid] = result
        return result

    def _mark_subtree_not_live(node: dict[str, Any]) -> None:
        nid = _node_id(node)
        if nid:
            contains_live[nid] = False
        for ch in _children(node):
            _mark_subtree_not_live(ch)

    compute_live(root)

    # Helper: does the subtree STRICTLY below `node` contain a live hit?
    # ("live" here is the suppression-aware liveness computed above, so a
    # deprecated node whose only descendants are suppressed is correctly NOT
    # rescued -- it collapses.)
    def descendants_contain_live(node: dict[str, Any]) -> bool:
        for ch in _children(node):
            if contains_live.get(_node_id(ch), False):
                return True
        return False

    # --- collect raw candidates ---
    suppress_cands: set[str] = set()
    deprecated_cands: set[str] = set()
    expand_cands: set[str] = set()

    root_id = _node_id(root)

    def collect(node: dict[str, Any]) -> None:
        nid = _node_id(node)
        f = flags_by_id.get(nid, {})

        # Suppression: suppress postfix OR global-never-expand. (Inert above the
        # root by construction — we only ever see the subtree rooted at root.)
        if f.get("has_suppress") or f.get("has_global_never_expand"):
            suppress_cands.add(nid)

        # Deprecated collapse — but NOT if a live in-slice descendant would be
        # hidden (the "don't collapse through live content" rule). The node
        # itself being deprecated does not count as live.
        if f.get("has_deprecated") and not descendants_contain_live(node):
            deprecated_cands.add(nid)

        # Auto-expand.
        if f.get("has_auto"):
            expand_cands.add(nid)

        for ch in _children(node):
            collect(ch)

    collect(root)

    # --- structural maps used by the merge + promotion passes ---
    def ancestors(nid: str) -> Iterable[str]:
        cur = parent_of.get(nid)
        guard = 0
        while cur is not None and guard < 4096:
            yield cur
            cur = parent_of.get(cur)
            guard += 1

    def topmost_only(cands: set[str]) -> set[str]:
        return {nid for nid in cands if not any(a in cands for a in ancestors(nid))}

    # ============================================================
    # DEPRECATED PRE-FILTER (the asymmetric merge's setup).
    # ============================================================
    # The merge is deliberately ASYMMETRIC: an EXPANSION ancestor may have
    # surviving CONTRACTION (suppress) descendants -- the everyday "expand a
    # broad region, then collapse a few noisy subtrees inside it" workflow.
    # Contraction-under-expansion is KEPT; only expansion-under-contraction is
    # dropped (see the merge below).
    #
    # That asymmetry makes "rootmost deprecated" dangerous to compute over the
    # whole tree, so we FIRST drop every deprecated candidate that has ANY
    # expansion- OR suppression-tagged ANCESTOR. Walk up from a deprecated node
    # to the first expansion/suppression ancestor:
    #   * If it is a SUPPRESSION ancestor, that subtree is hidden anyway, so
    #     the deprecated marker changes nothing.
    #   * If it is an EXPANSION ancestor, the expansion wins and the node is
    #     shown regardless of the deprecated marker (any hiding that does
    #     happen is owned by an independent suppression node, handled on its
    #     own).
    # Either way the deprecated marker is behaviorally INERT under an
    # expansion/suppression ancestor, so we remove it from contraction
    # consideration. The deprecated candidates that REMAIN are guaranteed to
    # have NO expansion/suppression ancestor, so the suppression-aware liveness
    # + topmost-only deprecated logic below runs cleanly on them.
    #
    # We test against the RAW suppress/expand candidate sets (any tagged node,
    # not just topmost) -- a deprecated node with ANY such ancestor at ANY
    # depth is inert. Self is not an ancestor; a node tagged BOTH deprecated
    # AND suppress/expand is resolved by same-node precedence elsewhere
    # (suppress > expand > deprecated), so it never lingers as a deprecated
    # contraction regardless.
    _operation_tagged = suppress_cands | expand_cands
    if _operation_tagged:
        deprecated_cands = {
            nid for nid in deprecated_cands
            if not any(a in _operation_tagged for a in ancestors(nid))
        }

    # children_of: parent uuid -> [child uuid, ...] in document order. Built
    # from parent_of (annotate recorded it for every real node). The promotion
    # pass and the outside-contraction liveness check walk DOWN via this map.
    children_of: dict[str, list[str]] = {}
    for nid in order:
        p = parent_of.get(nid)
        if p is not None:
            children_of.setdefault(p, []).append(nid)

    # node_is_live_self: is THIS node clean, non-deprecated live content that
    # should BLOCK a dead-limb collapse promotion? (Distinct from contains_live,
    # which is a subtree-rollup used only by the deprecated rescue.) The
    # DEPRECATED axis stays OPERATION-policy NAME+NOTE (classify flags: a
    # note-authored --deprecated deprecates the node), and the suppression axes
    # stay NAME-only via the flags. The ACTIVE axis honors match_scope
    # (post-Sol-audit follow-up): under 'name' a note-only family tag does NOT
    # make the node live-for-promotion (title-only slices are title-only
    # EVERYWHERE a membership-adjacent decision is made); under 'name_and_note'
    # the classify flag (NAME+NOTE) answers as before. NOTE: this deliberately
    # does NOT route through is_slice_member -- the presentation rule is
    # node-wide (ANY --deprecated postfix marks the node deprecated-content),
    # unlike per-prefix membership.
    _promotion_postfix_words = _recognized_postfix_words(
        auto_expand_postfix=auto_expand_postfix,
        suppress_postfix=suppress_postfix,
        deprecated_postfix=deprecated_postfix,
    )

    def node_is_live_self(nid: str) -> bool:
        f = flags_by_id.get(nid, {})
        if f.get("has_suppress") or f.get("has_global_never_expand"):
            return False
        if f.get("has_deprecated"):
            return False
        if match_scope == "name":
            node = node_by_id.get(nid)
            if not isinstance(node, dict):
                return False
            for t in _core_trailing_name_tags(node.get("name")):
                for p in active_prefixes:
                    if p and slice_tag_variant_match(
                        t, p, postfix_words=_promotion_postfix_words
                    ) is not None:
                        return True
            return False
        return bool(f.get("has_active"))

    # ============================================================
    # ASYMMETRIC OPERATION MERGE.
    # ============================================================
    # The merge is ASYMMETRIC so that the everyday case works: the user expands
    # a broad region (#X--expand) and then collapses a few noisy subtrees
    # INSIDE it (#Y--suppress).
    #
    #   * A rootmost CONTRACTION dominates any descendant EXPANSION (collapsing
    #     a subtree wins over an auto-expand nested inside it).
    #   * A rootmost EXPANSION does NOT dominate descendant CONTRACTIONS: a
    #     suppression nested under an expansion SURVIVES and collapses its own
    #     subtree. So the final shape can be "expansion region with a few
    #     suppressed pockets carved out of it."
    #
    # Resulting invariant:
    #     EXPANSION may contain CONTRACTION descendants.
    #     CONTRACTION may NOT contain EXPANSION descendants.
    #
    # Same-node precedence: suppress > expand > deprecated. A node tagged both
    # suppress and expand is treated as suppression only -- it is in
    # suppress_cands (so it lands in contraction_top) and is explicitly dropped
    # from the expansion set below; a node tagged both expand and deprecated
    # was already removed from deprecated_cands by the pre-filter above (and/or
    # is inert), so expand wins there.
    #
    # CONTRACTION = suppression ∪ (pre-filtered) deprecated, topmost-only.
    # A rootmost deprecated node has no live descendants (it passed the rescue
    # test) AND no expansion/suppression ancestor (the pre-filter guaranteed
    # it) -- so merging it with suppression and reducing topmost-only is
    # correct: it behaves identically to a suppression root for dominance.
    contraction_top = topmost_only(suppress_cands | deprecated_cands)
    expand_top = topmost_only(expand_cands)

    # CONTRACTION is taken WHOLE (it is never dominated by expansion).
    contraction_set = set(contraction_top)

    # EXPANSION drops any target that has a CONTRACTION ancestor-or-self.
    # ancestor: contraction dominates a nested expansion. self: a node tagged
    # both suppress and expand resolves to suppress (same-node precedence).
    expand_final: set[str] = set()
    dropped_expand: set[str] = set()
    for e in expand_top:
        if e in contraction_set or any(a in contraction_set for a in ancestors(e)):
            dropped_expand.add(e)
        else:
            expand_final.add(e)

    if dropped_expand:
        warnings.append(
            f"{len(dropped_expand)} expansion target(s) dropped: dominated by a "
            "contraction (suppress/deprecated) ancestor or by same-node "
            "suppress precedence."
        )

    # ============================================================
    # CONTRACTION UPWARD PROMOTION (dead-limb collapse).
    # ============================================================
    # A contraction target can sit at the BOTTOM of a chain of blank / dead
    # ancestors (e.g. live-ancestor -> blank -> blank -> deprecated leaf):
    # collapsing the leaf hides nothing useful and leaves the blank ancestors
    # hanging as a "dead limb". The collapse primitive cannot REMOVE individual
    # nodes, but it CAN choose a HIGHER collapse root: push each contraction
    # target up its ancestor chain as far as is safe, so the dead limb
    # collapses at the highest safe point (leaving at most ONE zombie node --
    # the highest one -- instead of a long chain).
    #
    # A contraction target under parent `p` promotes UP to `p` iff ALL hold:
    #   * `p` is not the slice/modal root,
    #   * `p` is NOT an expansion operation target,
    #   * `p` is NOT itself a live member that should be displayed,
    #   * `p` has NO live descendant OUTSIDE the subtrees already being
    #     contracted. When checking `p`'s descendants for liveness we treat any
    #     node currently in the contraction set as an OPAQUE, already-collapsed
    #     subtree and do NOT inspect inside it -- a stale/deprecated node under
    #     a contraction descendant is NOT a reason to keep `p` open. Only live
    #     content in a SIBLING branch (outside every contraction subtree)
    #     blocks promotion.
    # Done as an order-free FIXPOINT with ATOMIC CLUSTERING: when `p` absorbs a
    # child, ALL of `p`'s contraction descendants fold into `p` at once (so two
    # contraction children of an otherwise-dead `p` both promote together --
    # one sibling contraction must not be treated as "live" while promoting
    # another).
    #
    # PROMOTION RESTRICTION: a surviving CONTRACTION under a surviving EXPANSION
    # MUST NOT be upward-promoted AT ALL -- not merely "stopped at the
    # expansion boundary." The hazard: once the enclosing expansion rehydrates
    # its subtree from the cache, ancestors BETWEEN the expansion and the
    # contraction acquire their FULL cache children, which were NOT present in
    # the Phase-1 minimal slice tree this reasons over. Promoting the collapse
    # to any such ancestor would hide MORE than the explicitly-suppressed
    # subtree (e.g. "expand Core Details, suppress Broker" must collapse
    # exactly Broker, never some blank-looking parent that, post-expansion,
    # also holds other content). So contractions WITH an expansion ancestor
    # are FROZEN at their exact node; only contractions with NO expansion
    # ancestor are eligible for the dead-limb upward-promotion below (still
    # correct for ordinary suppress/deprecated roots outside any expansion
    # region).
    expand_set = set(expand_final)

    def _has_expansion_ancestor(nid: str) -> bool:
        return any(a in expand_set for a in ancestors(nid))

    # Partition: frozen (under an expansion -- never promote) vs promotable.
    frozen_under_expansion = {c for c in contraction_set if _has_expansion_ancestor(c)}
    promotable_contractions = contraction_set - frozen_under_expansion

    def subtree_has_live_outside_contractions(nid: str, contraction: set[str]) -> bool:
        # An already-contracting subtree is opaque: nothing inside it counts as
        # a reason to keep an ancestor open.
        if nid in contraction:
            return False
        if node_is_live_self(nid):
            return True
        for ch in children_of.get(nid, ()):
            if subtree_has_live_outside_contractions(ch, contraction):
                return True
        return False

    def contraction_descendants_of(pid: str, contraction: set[str]) -> set[str]:
        # Every contraction node at or below `pid` (so promoting to `pid`
        # folds the whole cluster, not just the one child we noticed).
        found: set[str] = set()
        stack = list(children_of.get(pid, ()))
        while stack:
            cur = stack.pop()
            if cur in contraction:
                found.add(cur)
            # Recurse regardless: a contraction node can have a deeper
            # contraction sibling-of-ancestor; but we never descend INTO an
            # already-found contraction's collapsed subtree for MORE
            # contractions (they'd be redundant). Keep it simple + correct:
            # only descend into non-contraction nodes.
            if cur not in contraction:
                stack.extend(children_of.get(cur, ()))
        return found

    def can_promote_to_parent(pid: str, contraction: set[str]) -> bool:
        if not pid or pid == root_id:
            return False
        if pid in expand_set:
            # A promotable contraction (the only kind this fixpoint moves) has
            # NO expansion ancestor by construction (contraction-under-
            # expansion was frozen before the loop), so a promotion target can
            # never be an expansion node. This branch is therefore unreachable
            # for the inputs we feed the loop; keep it as a hard defensive stop
            # (never promote a collapse onto an expansion target -- that would
            # collapse the region the expansion opened).
            return False
        if node_is_live_self(pid):
            return False
        # `p` may be promoted ONLY if no SIBLING branch (any child subtree not
        # itself a contraction target) holds live content. Children that ARE
        # contraction targets are opaque (they fold into `p`). The `contraction`
        # set passed in is the FULL contraction set (promotable + frozen), so a
        # frozen-under-expansion contraction elsewhere is also treated opaque;
        # in practice the two never share an ancestor below the root (frozen
        # ones sit under an expansion, promotable ones do not), so this is
        # belt-and-suspenders.
        for ch in children_of.get(pid, ()):
            if ch in contraction:
                continue
            if subtree_has_live_outside_contractions(ch, contraction):
                return False
        return True

    # Fixpoint: repeatedly find any PROMOTABLE contraction node whose parent
    # can absorb it and promote the whole cluster, until nothing moves.
    # Order-free (we always restart after a change) and terminating (each
    # promotion strictly raises a target toward the root; depth is bounded).
    # Only `promotable_contractions` (those with NO expansion ancestor) are
    # moved; `frozen_under_expansion` contractions are held at their exact node
    # and re-added after the loop. The liveness/opacity check still considers
    # the FULL contraction set (promotable | frozen) so a frozen contraction is
    # never mistaken for live blocking content.
    promotion_guard = 0
    changed = True
    while changed and promotion_guard < 100000:
        changed = False
        promotion_guard += 1
        full_contraction = promotable_contractions | frozen_under_expansion
        for c in list(promotable_contractions):
            p = parent_of.get(c)
            if p is None:
                continue
            if can_promote_to_parent(p, full_contraction):
                # Fold only the PROMOTABLE cluster under `p`; a frozen
                # contraction can never be under `p` here (it would require an
                # expansion ancestor, which would have blocked promotion), but
                # we intersect defensively so frozen targets are never moved.
                cluster = contraction_descendants_of(p, full_contraction)
                cluster.add(c)  # defensive; c is under p by construction
                cluster &= promotable_contractions
                promotable_contractions = (promotable_contractions - cluster) | {p}
                changed = True
                break

    # Recombine the promoted set with the frozen-under-expansion contractions,
    # then re-reduce to topmost-only (a promoted `p` may now sit above another
    # contraction node that did not itself promote). Frozen contractions are
    # under an expansion and promotable ones are not, so topmost_only never
    # collapses one into the other across the expansion boundary.
    contraction_set = topmost_only(promotable_contractions | frozen_under_expansion)

    # --- partition the unified contraction set back into the existing output
    #     shape (collapse_suppressed / collapse_deprecated) so Phase 3 and the
    #     client are untouched. A node is labeled 'suppressed' if it carries a
    #     suppress / global-never-expand flag; else 'deprecated'. A promoted
    #     blank ancestor carries NEITHER flag -- it is a pure structural
    #     collapse target; we label it 'deprecated' (an arbitrary-but-stable
    #     choice: both lists are executed identically as collapses by Phase 3,
    #     and 'deprecated' reads as "collapsed because the branch is dead").
    suppress_final: set[str] = set()
    deprecated_final: set[str] = set()
    for nid in contraction_set:
        f = flags_by_id.get(nid, {})
        if f.get("has_suppress") or f.get("has_global_never_expand"):
            suppress_final.add(nid)
        else:
            deprecated_final.add(nid)

    # ============================================================
    # DEPRECATED-BODY SUPPRESSION.
    # ============================================================
    # A node that carries the --deprecated postfix but SURVIVES into the final
    # render tree (it was RESCUED -- it has a live in-slice descendant, so it
    # was never added to the contraction set) renders HEADER-ONLY: its heading
    # stays (structure needed for its live descendants) but its NOTE BODY is
    # withheld (it is superseded by newer/summarized material nested beneath
    # it). The set of such surviving-deprecated node UUIDs is a DERIVED
    # PROJECTION FACT threaded to the composer as a side-channel render
    # directive (exactly like pending_collapse_ids) -- NOT per-node flags, NOT
    # note mutation, NOT a spec/render_scope field.
    #
    # DEFINITION (root-INCLUSIVE): a node whose classify_node_tags
    # `has_deprecated` is True, that is NOT itself a contraction target, and
    # that has NO contraction ANCESTOR (so it genuinely survives Phase-3
    # collapse -- a deprecated node under a suppress/deprecated collapse root
    # is dropped entirely and must NOT appear in this set). Computed from the
    # already-resolved contraction_set + the SAME flags_by_id, so no tags are
    # re-parsed. Root-inclusion is deliberate and independent of the
    # operation-target root-discard below: body-suppression is a render-body
    # policy, not an operation-target rule, so if the render ROOT itself is a
    # surviving deprecated node its body IS suppressed (its heading / abs-path
    # leader still render).
    body_suppressed_ids: set[str] = set()
    for nid in order:
        f = flags_by_id.get(nid, {})
        if not f.get("has_deprecated"):
            continue
        if nid in contraction_set:
            continue
        if any(a in contraction_set for a in ancestors(nid)):
            continue
        body_suppressed_ids.add(nid)

    # Root expansion is allowed; root contraction is still never an operation target.
    for s in (suppress_final, deprecated_final):
        s.discard(root_id)

    # Stable output order (document/annotation order).
    def in_order(s: set[str]) -> list[str]:
        return [nid for nid in order if nid in s]

    # ============================================================
    # TIER-2 DEFERRED-ON-SCROLL GATING MAP.
    # ============================================================
    # The Tier-2 'sync the live Workflowy GUI to the modal as you scroll'
    # watcher fires each collapse/expand op by UUID as its row materializes
    # during the user's natural reading-scroll. Because a CONTRACTION may sit
    # UNDER an EXPANSION (the asymmetric merge), a collapse nested inside an
    # expansion region MUST NOT fire/complete until its enclosing expansion has
    # fired+completed -- otherwise it would collapse a node whose subtree the
    # expansion has not yet rehydrated, or the later expand-cascade would
    # re-open it.
    #
    # So we emit, for the client watcher, the per-collapse GATING MAP: each
    # collapse target -> the list of its EXPANSION ancestors (from expand_final)
    # that must fire FIRST. A collapse with NO expansion ancestor maps to an
    # EMPTY list -> it fires freely in whatever scroll order reveals it
    # (order-independent). A collapse UNDER an expansion lists the expansion(s)
    # the watcher must see fired before it may collapse. (If the user never
    # scrolls far enough to trigger the gating expansion, the gated collapse
    # simply never fires -- acceptable.)
    #
    # Pure UUID metadata; the client consumes it. Expand targets carry no gate
    # (expansion is always safe to fire on materialize -- it is additive and
    # never sits under a contraction).
    expand_lookup = set(expand_final)
    collapse_all = suppress_final | deprecated_final
    pending_collapse_gated_by: dict[str, list[str]] = {}
    for c in collapse_all:
        gating = [a for a in ancestors(c) if a in expand_lookup]
        # ancestors() yields nearest-first; reverse to rootmost-first so the
        # client can reason top-down if it wants. Order is not load-bearing
        # (the client just needs ALL of them fired), but rootmost-first reads
        # naturally.
        pending_collapse_gated_by[c] = list(reversed(gating))

    return {
        "collapse_suppressed": in_order(suppress_final),
        "collapse_deprecated": in_order(deprecated_final),
        "expand": in_order(expand_final),
        # --- Tier-2 deferred-on-scroll metadata (consumed by the client
        #     'active slice watcher'; harmless/ignored by Tier-1 render). ---
        "pending_expand_ids": in_order(expand_final),
        "pending_collapse_ids": in_order(collapse_all),
        "pending_collapse_gated_by": {
            c: pending_collapse_gated_by[c] for c in in_order(collapse_all)
        },
        # --- DEPRECATED-BODY SUPPRESSION set. Surviving deprecated nodes
        #     render HEADER-ONLY; the composer withholds their note body.
        #     Root-inclusive (NOT subject to the operation-target root-discard
        #     above). A DERIVED projection fact threaded to the composer as an
        #     operand (like pending_collapse_ids), never a spec field. ---
        "body_suppressed_ids": in_order(body_suppressed_ids),
        "warnings": warnings,
    }


# ===========================================================================
# PHASE 3 — apply an expansion plan to a subtree (server-side DOM emulation)
# ===========================================================================

# @beacon[
#   id=f8policy@phase3-apply-plan,
#   slice_labels=slices,
#   kind=ast,
#   comment=PHASE 3 (no-DOM path) -- apply a Phase-2 expansion plan to the Phase-1 membership subtree IN MEMORY, emulating the browser's executeF8ExpansionPlan against the live DOM. Mandated order EXPAND FIRST, THEN COLLAPSE: (1) expand, (2) collapse_suppressed, (3) collapse_deprecated -- expanding first so a contraction nested inside an expanded region survives (collapse-first would let the expansion re-hydrate and overwrite the nested collapse). COLLAPSE (both sets, identical effect once a UUID is in hand) = drop every descendant of the target node from the subtree (node stays as a childless leaf), mirroring WF.collapseItem. EXPAND = re-hydrate the target node's FULL descendant subtree from the /nodes-export cache (priority-ordered, INCLUDING non-members -- exactly as opening a bullet in the GUI reveals every child regardless of tags), mirroring the recursive WF.expandItem. LOAD-BEARING BOUNDARY SEAL: boundary_predicate + boundary_root_uuid (default None) make the EXPAND hydration honor the SAME stop-at-child-anchors boundary Phase 1 used -- without it, an --expand ancestor of a child anchor would silently re-inflate from the raw cache the very subtree Phase 1 pruned (Phase 1 stopping is not enough because hydration reads raw cache). A child-anchor node hydrates as a childless leaf; boundary_root_uuid exempts the projection root so expanding AT a boundary still reveals its immediate children. LOAD-BEARING GLOBAL-NEVER SEAL (HYDSUP): global_never_expand_tags (default (); EXACT-match, TRAILING-tag-region stems via name_has_global_never_tag, mirroring node_has_suppress_ancestor -- 084: a mid-title or note-only global tag never suppresses, under every scope) makes the EXPAND hydration skip + NOT-descend into any child carrying a global-never (e.g. wfy-suppress) tag -- without it, a globally-suppressed node ABSENT from the Phase-1 minimal tree (so Phase 2 never classified it) was silently re-grafted from the raw cache under an --expand ancestor (the Phase-3 hydration leak, fixed for persisted slices AND Lightning render_tags postfix_policy='slice'). Tested on the CHILD only, so the expand TARGET itself is exempt -- same construction as the completed seal. The result REPLACES the subtree so a slice-group GLIMPSE renders exactly what an F8'd live GLIMPSE of the same slice renders. Returns a NEW subtree (input not mutated).,
# ]
def apply_expansion_plan_to_subtree(
    subtree: dict[str, Any],
    plan: dict[str, Any],
    cache_by_id: dict[str, dict[str, Any]],
    *,
    exclude_completed: bool = False,
    boundary_predicate: "Callable[[dict[str, Any]], bool] | None" = None,
    boundary_root_uuid: str | None = None,
    global_never_expand_tags: Iterable[str] = (),
) -> dict[str, Any]:
    """Apply an F8 expansion plan to a subtree in memory (Phase 3, no DOM).

    This is the server-side twin of the browser's ``executeF8ExpansionPlan``.
    The live client realizes the plan's UUID sets against the live DOM
    (``collapseSubtreeRootById`` / ``expandSubtreeSafeById``); here we realize
    the SAME sets against the in-memory ``subtree`` produced by
    ``build_slice_subtree_from_cache``, so that a cache-sourced slice-group
    GLIMPSE renders exactly what an F8'd live-DOM GLIMPSE of the same slice
    would render.

    Order (mandated, matches the client) -- EXPAND FIRST, THEN COLLAPSE:
      1. ``expand``              -- re-hydrate each target's FULL descendant
                                    subtree from ``cache_by_id``.
      2. ``collapse_suppressed`` -- collapse each target (drop its descendants).
      3. ``collapse_deprecated`` -- collapse each target (drop its descendants).

    WHY EXPAND-FIRST (the asymmetry): a CONTRACTION may legitimately sit UNDER
    an EXPANSION (expand a broad region, then suppress a few pockets inside
    it). If we collapsed first and expanded second, the expansion would
    re-hydrate the whole region from cache and OVERWRITE (undo) the nested
    collapse. Expanding FIRST lays down the full region; the subsequent
    collapses then carve out exactly the suppressed/deprecated pockets and
    stick. A contraction with NO expansion ancestor is unaffected by order
    (nothing re-hydrates it). The plan guarantees no EXPANSION sits under a
    CONTRACTION, so a later collapse never wipes an expansion that should have
    survived.

    COLLAPSE semantics: a collapsed node remains present as a CHILDLESS leaf;
    only its descendants are removed (``WF.collapseItem`` hides the subtree but
    not the node). The plan's collapse sets are topmost-only WITHIN their
    region, so a single prune per target suffices. NOTE: on the CACHE-PROJECTED
    render paths (slice-group GLIMPSE + Apply-slice modal) a subsequent Phase 4
    (:func:`prune_collapsed_leaf_zombies`, invoked via
    :func:`build_stage1_base_tree`) REMOVES these childless zombie leaves
    from the render tree -- a deliberate, isolated divergence from the live
    Workflowy expansion state (the live DOM, and the DOM-faithful live GLIMPSE /
    F8, MUST keep them because no expand/contract op can remove a collapsed node
    itself). Phase 3 itself leaves them in place; Phase 4 is what drops them.

    EXPAND semantics: the target's children are rebuilt from the cache's full
    descendant subtree, priority-ordered, INCLUDING nodes that are not slice
    members -- exactly as expanding a bullet in Workflowy reveals every child
    regardless of tags.

    ``exclude_completed`` (default False -- see the COMPLETED-NODE EXCLUSION
    POLICY block above build_slice_subtree_from_cache): when True, the EXPAND
    re-hydration (``_hydrate_from_cache``) applies the SAME admit-time prune
    Phase 1 does -- it does NOT descend into a completed cache child. THIS IS
    LOAD-BEARING: Phase-1 membership excludes completed nodes, but an
    ``--expand`` node that is an ANCESTOR of a completed subtree would, without
    this, re-inflate that completed subtree straight from the raw cache during
    expansion -- silently undoing the Phase-1 exclusion (the auto-expand policy
    otherwise reveals EVERY child regardless of tags/state). Threading the same
    completed guard into the hydration walk keeps 'a completed subtree is hidden
    even under an --expand ancestor' true. The expand TARGET itself is exempt by
    the same construction as Phase 1's root: hydration tests the flag only when
    deciding whether to descend into a CHILD, so expanding a completed node
    still reveals its (non-completed) children.

    ``global_never_expand_tags`` (default ``()``, HYDSUP): the live
    ``f8_global_never_expand_tags`` list (e.g. ``['wfy-suppress']``), threaded
    into the EXPAND hydration walk with the SAME shape as the completed seal
    above -- a LOAD-BEARING leak fix, not belt-and-suspenders. A node carrying
    a global-never tag that is NOT a slice member never enters the Phase-1
    minimal subtree, so Phase 2 never classifies it and no contraction op
    covers it -- yet an ``--expand`` ANCESTOR of it would, without this seal,
    re-hydrate it straight from the raw cache (EXPAND reveals every child
    regardless of tags). During hydration, any CHILD whose NAME carries an
    EXACT-match global-never stem (``name_has_global_never_tag``, the name's
    TRAILING tag region; the SAME trailing-region,
    exact-match semantics as :func:`node_has_suppress_ancestor`, so the config
    field means one thing everywhere) is skipped entirely -- neither grafted
    nor descended into. Tested on the CHILD, so the expand TARGET itself is
    exempt (same root-exempt construction as the completed seal). Phase 2's
    own global-never classification (over the Phase-1 tree) is unchanged;
    this seal covers exactly the nodes Phase 2 cannot see. Default ``()`` is
    a no-op, so no other caller changes.

    Returns a NEW nested ``{id,name,note,children:[...]}`` subtree; the input
    ``subtree`` is never mutated.
    """
    if not isinstance(subtree, dict):
        return subtree

    collapse_suppressed = list(plan.get("collapse_suppressed") or [])
    collapse_deprecated = list(plan.get("collapse_deprecated") or [])
    expand = list(plan.get("expand") or [])

    # GLOBAL-NEVER-EXPAND stems (HYDSUP), via the ONE shared doctrine
    # helper (normalize_global_never_stems): '#'-optional, whitespace-trimmed,
    # EXACT-match NAME-only stems (NOT family/prefix) -- so the SAME config
    # field means the SAME thing in Phase 2, the render_symbols resolution
    # filter, the render_symbols expand sweep, and this Phase-3 hydration
    # seal (no semantic seam). Empty set -> the hydration guard below is a
    # no-op.
    _never_stems = normalize_global_never_stems(global_never_expand_tags)

    # Pre-index the cache's children by parent_id (priority-sorted), the same
    # way build_slice_subtree_from_cache does, so EXPAND hydration produces
    # identical document order to a Phase-1 build.
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for node in cache_by_id.values():
        pid = node.get("parent_id")
        if pid is not None:
            children_by_parent.setdefault(str(pid), []).append(node)
    for kids in children_by_parent.values():
        kids.sort(
            key=lambda n: (
                n.get("priority") if isinstance(n.get("priority"), (int, float)) else 0
            )
        )

    # Deep-ish copy of the input subtree (shallow per-node dict + fresh children
    # lists) so we never mutate the caller's tree.
    def _clone(node: dict[str, Any]) -> dict[str, Any]:
        out = {
            "id": node.get("id"),
            "name": node.get("name"),
            "note": node.get("note"),
            "children": [
                _clone(ch) for ch in (node.get("children") or []) if isinstance(ch, dict)
            ],
        }
        return out

    root = _clone(subtree)

    # Build a uuid -> node index over the working tree for O(1) target lookup.
    index: dict[str, dict[str, Any]] = {}

    def _reindex(node: dict[str, Any]) -> None:
        nid = _node_id(node)
        if nid:
            index[nid] = node
        for ch in node.get("children") or []:
            _reindex(ch)

    _reindex(root)

    # --- COLLAPSE: drop descendants of each target present in the tree. ---
    def _collapse(uuid: str) -> None:
        node = index.get(uuid)
        if node is None:
            return  # target not in the (pruned) subtree -> nothing to collapse.
        # Remove the descendant nodes from the index, then clear children.
        def _deindex(n: dict[str, Any]) -> None:
            for ch in n.get("children") or []:
                cid = _node_id(ch)
                if cid:
                    index.pop(cid, None)
                _deindex(ch)
        _deindex(node)
        node["children"] = []

    # --- EXPAND FIRST: re-hydrate full cache descendants under each target. ---
    # (Must run BEFORE the collapses so a contraction nested inside an expanded
    # region survives -- see the order rationale in the docstring.)
    def _is_boundary_uuid(nid: str) -> bool:
        if boundary_predicate is None:
            return False
        if boundary_root_uuid is not None and str(nid) == str(boundary_root_uuid):
            return False
        node = cache_by_id.get(str(nid))
        return bool(node is not None and boundary_predicate(node))

    def _hydrate_from_cache(nid: str) -> list[dict[str, Any]]:
        # STOP-RECURSION boundary (descend_policy='stop_at_child_anchors'). If
        # the expansion target itself is a child-anchor boundary, it stays a
        # leaf even when it carries --expand; if one of its children is a
        # boundary, include that child as a leaf but do not hydrate below it.
        # This is the Phase-3 counterpart of Phase-1's boundary walk and
        # prevents --expand from silently re-inflating a pruned child-anchor
        # subtree.
        if _is_boundary_uuid(nid):
            return []
        out: list[dict[str, Any]] = []
        for ch in children_by_parent.get(nid, []):
            cid = _node_id(ch)
            if not cid:
                continue
            # COMPLETED-NODE EXCLUSION (default off). LOAD-BEARING: EXPAND
            # re-hydrates straight from the RAW cache, so without this an
            # --expand ANCESTOR of a completed subtree would re-inflate that
            # subtree and undo the Phase-1 exclusion. Skip a completed CHILD
            # entirely (do not add it, do not descend). Tested on the CHILD, so
            # the expand TARGET itself is exempt -- expanding a completed node
            # still reveals its non-completed children (same root-exempt
            # construction as Phase 1's collect()). See the POLICY block above
            # build_slice_subtree_from_cache.
            if exclude_completed and _node_is_completed(ch):
                continue
            # GLOBAL-NEVER-EXPAND SUPPRESSION SEAL (HYDSUP; default (): no-op).
            # LOAD-BEARING, same shape as the completed seal above: EXPAND
            # re-hydrates straight from the RAW cache, so without this an
            # --expand ANCESTOR of a globally-suppressed subtree (e.g.
            # #wfy-suppress) that was ABSENT from the Phase-1 minimal tree
            # (and therefore never classified by Phase 2) would re-inflate
            # that subtree here. Skip a suppressed CHILD entirely (do not add
            # it, do not descend) -- EXACT-match, NAME-only, mirroring
            # node_has_suppress_ancestor. Tested on the CHILD, so the expand
            # TARGET itself is exempt (same construction as the completed
            # seal / Phase 1's root).
            if name_has_global_never_tag(ch.get("name"), _never_stems):
                continue
            out.append(
                {
                    "id": cid,
                    "name": ch.get("name"),
                    "note": ch.get("note"),
                    "children": [] if _is_boundary_uuid(cid) else _hydrate_from_cache(cid),
                }
            )
        return out

    def _expand(uuid: str) -> None:
        node = index.get(uuid)
        if node is None:
            return  # target not in the tree -> nothing to expand here.
        hydrated = _hydrate_from_cache(uuid)
        node["children"] = hydrated
        # Re-index the newly grafted descendants so a later collapse can find
        # (and prune) a suppressed/deprecated pocket inside the expanded region.
        for ch in hydrated:
            _reindex(ch)

    for uuid in expand:
        _expand(uuid)

    # --- THEN COLLAPSE: prune descendants of each target present in the tree
    #     (including pockets just rehydrated by an enclosing expansion). ---
    for uuid in collapse_suppressed:
        _collapse(uuid)
    for uuid in collapse_deprecated:
        _collapse(uuid)

    return root


# ===========================================================================
# PHASE 4 — RENDER-ONLY zombie prune (cache-projected slice renders ONLY)
# ===========================================================================

class SliceProjectionInvariantError(RuntimeError):
    """An upstream slice-projection invariant was violated.

    Raised by :func:`prune_collapsed_leaf_zombies` (Phase 4) when a node in the
    contraction set (``plan['pending_collapse_ids']``) is present in the
    post-Phase-3 subtree but STILL HAS CHILDREN. That is impossible if Phases
    1-3 are correct (a contraction target has no live descendants by the
    rescue rule, no expansion ever sits under a contraction by the asymmetric
    merge, and Phase 3 collapses each contraction target to a childless leaf),
    so it signals a genuine upstream logic error -- NOT a case to paper over.
    Both cache-projected callers catch it and return a clear structured
    failure (fail-closed, loud, diagnosable) rather than emitting a corrupt
    render.
    """


# @beacon[
#   id=f8policy@phase4-prune-zombies,
#   slice_labels=nexus--md-preview,slices,
#   kind=ast,
#   comment=PHASE 4 (RENDER-ONLY, cache-projected-slice ONLY) -- remove the collapsed contraction nodes that Phase 3 left behind as CHILDLESS ZOMBIE LEAVES. Phase 3 mirrors WF.collapseItem, which hides a node's descendants but leaves the node ITSELF present as a childless leaf; there is NO expand/contract DOM op that removes the node itself, so the live Workflowy GUI (and the live-DOM GLIMPSE / F8, which must match it) legitimately keeps these leaves. Phase 4 is the DELIBERATE, ISOLATED divergence: on the cache-projected render path ONLY (slice-group GLIMPSE + Apply-slice modal), it drops those leaves from the RENDER TREE so a slice that deprecated hundreds of leaves actually shrinks. Fed the UNIFIED contraction set plan['pending_collapse_ids'] (suppress_final | deprecated_final; topmost-only; root already discarded) -- it does NOT re-derive tags or re-stratify suppress vs deprecated (both are identical collapses). INVARIANT (asserted, fail-loud via SliceProjectionInvariantError): every contraction node PRESENT in the tree is childless; a present-and-has-children contraction node is an upstream logic error. A contraction UUID simply ABSENT from the tree (e.g. nested under another collapse target) is normal and skipped. NEVER runs on the raw root_uuid dump (which has no plan). Returns {subtree, pruned_zombie_count} (new tree; input untouched).,
# ]
def prune_collapsed_leaf_zombies(
    subtree: dict[str, Any],
    pending_collapse_ids: Iterable[str],
) -> dict[str, Any]:
    """Remove Phase-3 collapsed contraction nodes from the RENDER tree (Phase 4).

    THE DIVERGENCE (deliberate, isolated, documented). Phase 3 emulates
    ``WF.collapseItem``: it drops a collapse target's DESCENDANTS but leaves the
    target itself as a CHILDLESS LEAF, because that is exactly what the live
    Workflowy DOM does and there is no expand/contract operation that can remove
    a collapsed node itself. The live-DOM GLIMPSE and F8 therefore MUST keep
    these childless leaves (to stay byte-for-byte faithful to the GUI). But on
    the CACHE-PROJECTED render path -- the slice-group GLIMPSE and the
    Apply-slice docked modal -- we are rendering a standalone Markdown
    pseudo-document, not mirroring the GUI's expansion state, so we can and DO
    remove them. Without this, deprecating hundreds of leaves to shrink a slice
    accomplishes nothing (the leaves still render); with it, the slice actually
    shrinks. This is the ONE sanctioned place the cache-projected render output
    diverges from the live Workflowy expansion state.

    INPUT -- the UNIFIED contraction set. ``pending_collapse_ids`` is the
    plan's ``suppress_final | deprecated_final`` (topmost-only within its
    region, root already discarded by Phase 2). BOTH suppress and deprecated
    zombies are dropped, and because Phase 2/3 execute the two sets identically
    there is nothing to stratify -- we treat the set as one opaque
    "contraction" set and never re-derive tags at render time. (Some entries
    may be PROMOTED BLANK ANCESTORS from Phase 2's dead-limb upward promotion;
    that is expected and correct -- they are still childless contraction roots
    and are pruned identically.)

    INVARIANT (asserted, fail-loud). Every contraction node that is PRESENT in
    the post-Phase-3 subtree is childless -- guaranteed by the rescue rule (a
    contraction target has no live descendants), the asymmetric merge (no
    expansion under a contraction), and Phase 3's collapse-to-leaf. If a
    present contraction node STILL HAS CHILDREN, an upstream invariant broke;
    we raise :class:`SliceProjectionInvariantError` rather than silently prune
    live content or silently keep a zombie. A contraction UUID that is simply
    ABSENT from the tree (legitimately: it sits under another collapse target
    and was removed with that target's descendants) is NOT an error -- it is
    skipped.

    Returns ``{"subtree": <new tree>, "pruned_zombie_count": int}``. The input
    ``subtree`` is never mutated (new node dicts + fresh children lists).
    """
    contraction: set[str] = {str(u) for u in (pending_collapse_ids or []) if u}
    root_id = _node_id(subtree) if isinstance(subtree, dict) else None

    if not isinstance(subtree, dict):
        return {"subtree": subtree, "pruned_zombie_count": 0}

    # Defensive: the render root must NEVER be a contraction target -- Phase 2
    # discards root_id from every op set. If it somehow appears, that is an
    # upstream invariant break; fail loud rather than prune the whole tree to
    # nothing.
    if root_id is not None and root_id in contraction:
        raise SliceProjectionInvariantError(
            f"render root {root_id!r} is in the contraction set; Phase 2 must "
            "discard the root from every operation set. Upstream invariant "
            "violated -- refusing to prune the entire tree."
        )

    pruned = 0

    def _rebuild(node: dict[str, Any]) -> dict[str, Any]:
        nonlocal pruned
        kept_children: list[dict[str, Any]] = []
        for ch in (node.get("children") or []):
            if not isinstance(ch, dict):
                continue
            cid = _node_id(ch)
            if cid and cid in contraction:
                # This child is a collapsed contraction node. It MUST be a
                # childless zombie (Phase-3 collapse-to-leaf); if it still has
                # children an upstream invariant broke -> fail loud.
                if ch.get("children"):
                    raise SliceProjectionInvariantError(
                        f"contraction node {cid!r} is present with "
                        f"{len(ch.get('children') or [])} surviving child(ren) "
                        "after Phase 3; a collapse target must be childless. "
                        "Upstream invariant violated (rescue rule / "
                        "no-expansion-under-contraction / collapse-to-leaf)."
                    )
                # Drop the zombie: do NOT copy it into kept_children.
                pruned += 1
                continue
            kept_children.append(_rebuild(ch))
        return {
            "id": node.get("id"),
            "name": node.get("name"),
            "note": node.get("note"),
            "children": kept_children,
        }

    new_root = _rebuild(subtree)
    return {"subtree": new_root, "pruned_zombie_count": pruned}


# ===========================================================================
# SHARED SLICE PROJECTION FUNNEL — Phase 1 -> 2 -> 3 -> 4, in ONE place
# ===========================================================================

# 💡 V2 / FUTURE IDEA (deferred; DO NOT implement unless Dan explicitly asks) ---
# ---------------------------------------------------------------------------
# ⚠️ This is a beacon-slice to-do note, not a live spec. An agent reading the
# #f8-expand / #nexus--md-preview slice will see it exactly where it is
# actionable; it is intentionally speculative and LOW PRIORITY. Do not act on
# it uninvited.
#
# GOAL: dissolve the ONE remaining completed-exclusion divergence between the
# cache-projected RENDER (this funnel's output -> the pushed Markdown modal /
# slice-group GLIMPSE) and the LIVE Workflowy main pane after a ⟳ Sync View.
# The divergence exists because exclude_completed DELETES completed nodes from
# the render tree (Phase 1 admit-prune + Phase 3 hydration-prune) -- a
# render-tree operation with NO Workflowy-DOM-API equivalent (the live GUI can
# only COLLAPSE a node, never remove it, and its own show/hide-completed toggle
# is the user's, not ours). So the live-GUI-sync watcher can never reproduce a
# DELETION, and completed nodes may still show in the main pane (esp. with the
# user's "show completed" toggle on).
#
# THE SKETCH (algorithmically equivalent END-STATE for completed nodes, via a
# path the DOM API CAN drive): run TWO parallel projections through this same
# Phase 1->4 pipeline instead of one --
#   * TREE A = the first-class RENDER tree (today's behavior): completed nodes
#     truly excluded/deleted. This stays the canonical, correct output the
#     agent/modal consumes. UNCHANGED.
#   * TREE B = a SYNC-PLAN tree that does NOT delete completed nodes but instead
#     folds them into the CONTRACTION set as an extra class of collapse targets
#     (topmost-completed within each branch), so Phase 2/3 emit the extra
#     collapse ops that, applied to the live DOM, hide the completed subtrees
#     the same way the render omits them. The watcher then issues those
#     collapses alongside the existing suppress/deprecated ones.
# Net: the render is unchanged; the live-GUI sync gains the ops needed to reach
# a completed-equivalent shape WITHOUT a delete primitive. It is deliberately
# LESS optimal (collapse-not-delete) and must be handled carefully (interaction
# with --expand ancestors: a completed collapse target nested under an expansion
# needs the SAME asymmetric gating the existing contraction set already has, so
# reusing pending_collapse_gated_by is the natural fit; the completed collapse
# leaves a childless zombie in the live GUI exactly like today's suppress path,
# which is already an accepted divergence).
#
# WHY IT'S LOW PRIORITY: this is a near-non-issue for UX. The status-bar caveat
# ("the main Workflowy view may render slightly differently than this pushed
# view") already covers it, and the user can reach the render's view in ONE
# CLICK by toggling Workflowy's own show/hide-completed button -- which some
# users may even PREFER over having us pre-collapse the completed nodes for
# them. So: a clean-architecture nice-to-have, not a fix. If pursued, keep the
# ONE-composer / single-funnel discipline: TREE B must flow through this SAME
# function (a second call / a mode flag), never a forked parallel pipeline.
# ---------------------------------------------------------------------------

# @beacon[
#   id=f8policy@build-stage1-base-tree,
#   slice_labels=nexus--md-preview,slices,
#   kind=ast,
#   comment=STAGE 1 (the base tree): the SHARED cache-projected slice funnel -- the ONE place the full STAGE-1 base-tree projection Phase 1 (build_slice_subtree_from_cache) -> Phase 2 (build_expansion_plan) -> Phase 3 (apply_expansion_plan_to_subtree) -> Phase 4 (prune_collapsed_leaf_zombies) lives (membership + expand/suppress/deprecated + completed-exclusion + anchor-boundary + global-never hydration seal + zombie prune). STAGE 2 (filter(since)+sort+render) runs LATER, in render_stage2_filter_sort; this function is purely STAGE 1. BOTH cache-projected consumers call it: the slice-group GLIMPSE (_glimpse_slice_group_from_cache) AND the Apply-slice docked modal (_handle_render_slice_modal) -- AND, under postfix_policy='slice', the Lightning Rod render_tags projectors (ap:IMPPTX) -- which previously hand-duplicated the identical three-call sequence and had to be kept in sync by hand -- so Phase 4 (and any future projection change) can NEVER diverge between GLIMPSE-slice and modal-slice by construction. boundary_predicate (Phase-C descend_policy='stop_at_child_anchors', default None) threads the stop-at-child-anchors traversal into BOTH Phase 1 (admit-time boundary) AND Phase 3 (hydration boundary, the load-bearing seal against --expand re-inflating a pruned child anchor) via boundary_root_uuid=root_uuid, so the boundary holds across the whole funnel by construction. global_never_expand_tags threads into Phase 2 classification AND Phase 3 hydration (HYDSUP: a globally-suppressed node outside the Phase-1 minimal tree can no longer be re-grafted from raw cache under an --expand ancestor). Returns (final_subtree, plan, prune_meta); final_subtree is None iff Phase 1 found no such root (caller reports the cache-miss). prune_meta carries pruned_zombie_count for observability/warning rows. Deliberately NOT used by the raw root_uuid dump (member_all=True, no plan, no Phase 2/3/4). Raises SliceProjectionInvariantError up through Phase 4 on a broken upstream invariant so callers fail closed + loud.,
# ]
def build_stage1_base_tree(
    cache_by_id: dict[str, dict[str, Any]],
    root_uuid: str,
    active_prefixes: Iterable[str],
    *,
    auto_expand_postfix: str = DEFAULT_AUTO_EXPAND_POSTFIX,
    suppress_postfix: str = DEFAULT_SUPPRESS_POSTFIX,
    deprecated_postfix: str = DEFAULT_DEPRECATED_POSTFIX,
    global_never_expand_tags: Iterable[str] = (),
    exclude_completed: bool = False,
    match_scope: str = "name_and_note",
    boundary_predicate: "Callable[[dict[str, Any]], bool] | None" = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]:
    """Build the STAGE-1 base tree for a slice group from the cache (Phases 1-4).

    STAGE 1 = the base tree: membership + expand/suppress/deprecated +
    completed-exclusion + anchor-boundary + zombie prune, i.e. the four
    Phases below. STAGE 2 (the ``since`` filter + sort + render) is a SEPARATE
    step that runs afterward in ``render_stage2_filter_sort`` over this base
    tree; this function does none of it.

    STAGE 1 = STRUCTURAL MEMBERSHIP. This funnel decides membership by TAG
    (the slice prefixes). Two SIBLING Stage-1 membership predicates exist for
    the other cache-sourced GLIMPSE sources -- ``member_all`` (the raw-dump
    root: everything) and the Lightning Rod TAG/TEXT-SEARCH/SYMBOL predicate
    (fed as ``member_predicate`` into ``build_selected_subtree_from_cache``).
    ALL of these -- including Lightning Rod's free-text/regex SEARCH -- are
    STAGE 1: they decide WHICH nodes are IN the base tree by content/identity.
    (Kept in Stage 1 for v1 by design -- see NXSTAGE. The known cost is that
    search + base-tree are FUSED, so one cannot hold a base tree and sweep many
    searches without re-projecting; not a v1 need. REVISIT TRIGGER: a real
    'same base tree, many searches' use case is when to lift search into a
    Stage-2 post-membership content filter.) Consequently STAGE 2's "filter"
    (``since``) means a chronological/positional TRIM of the already-decided
    set ONLY -- it is explicitly NOT a content search and never re-decides
    membership.

    The SINGLE shared projection funnel for the cache-projected slice
    consumers -- the slice-group GLIMPSE (``_glimpse_slice_group_from_cache``),
    the Apply-slice docked modal (``_handle_render_slice_modal``), AND, under
    ``postfix_policy='slice'`` (ap:IMPPTX), the Lightning Rod render_tags
    projectors. The first two previously inlined the identical Phase 1 -> 2 ->
    3 sequence and had to be kept in lockstep by hand; centralizing them here
    means Phase 4 (and any future projection change) applies to every consumer
    by construction, so the cache-projected renders can never drift.

    Runs, in order:
      * Phase 1 -- ``build_slice_subtree_from_cache`` (minimal membership
        subtree). Returns ``(None, None, {...})`` if ``root_uuid`` is absent
        from the cache, so the caller can emit its own cache-miss message.
      * Phase 2 -- ``build_expansion_plan`` (the suppress/deprecated/expand
        plan + the unified ``pending_collapse_ids`` contraction set).
      * Phase 3 -- ``apply_expansion_plan_to_subtree`` (collapse-to-leaf +
        expand rehydration, in memory).
      * Phase 4 -- ``prune_collapsed_leaf_zombies`` (drop the childless
        contraction zombies from the RENDER tree; the deliberate
        cache-render-only divergence from the live Workflowy expansion state).

    Returns ``(final_subtree, plan, prune_meta)``:
      * ``final_subtree`` -- the post-Phase-4 render tree, or ``None`` when
        Phase 1 found no such root.
      * ``plan`` -- the Phase-2 plan (``None`` when Phase 1 found no root), so
        callers can forward ``warnings`` / the Tier-2 ``pending_*`` metadata.
      * ``prune_meta`` -- ``{"pruned_zombie_count": int}`` for observability
        (GLIMPSE payload, modal info row, logs). Zero when Phase 1 found no
        root.

    Raises :class:`SliceProjectionInvariantError` (up through Phase 4) if a
    contraction node survives Phase 3 with children -- a broken upstream
    invariant. Callers catch it and fail closed with a structured error.

    NOTE: this funnel is for the SLICE projection only. The raw ``root_uuid``
    dump path (``member_all=True``) has no slice semantics -- no Phase 2/3/4 --
    and deliberately does NOT use this helper.

    ``exclude_completed`` (server-side GLIMPSE completed-node policy, default
    False) is threaded into BOTH Phase 1 (build_slice_subtree_from_cache, the
    admit-time prune) AND Phase 3 (apply_expansion_plan_to_subtree, the EXPAND
    hydration walk) -- it is NOT a pure Phase-1 pass-through. Phase 1 excludes
    completed subtrees at admit time, but Phase 3's EXPAND re-hydrates straight
    from the RAW cache, so an ``--expand`` node that is an ANCESTOR of a
    completed subtree would silently re-inflate the very subtree Phase 1
    excluded unless the SAME completed guard is applied during hydration. So
    the flag is LOAD-BEARING in Phase 3 (see apply_expansion_plan_to_subtree's
    exclude_completed docstring), not belt-and-suspenders. Phase 2 and Phase 4
    need no completed flag (Phase 2 reasons over the already-pruned Phase-1
    subtree; Phase 4 is a pure structural prune). See the COMPLETED-NODE
    EXCLUSION POLICY block above build_slice_subtree_from_cache.

    ``match_scope`` (SELECTION-stage scope knob, default ``"name_and_note"``)
    feeds the THREE membership-adjacent questions in the funnel: Phase 1's
    admission predicate, Phase 2's deprecated-rescue liveness, AND Phase 2's
    promotion ACTIVE-self check (all membership questions -- post-Sol-audit
    Finding 2 + follow-up: a title-only slice must not have a deprecated
    branch rescued by a note-only tag, nor a collapse promotion blocked by
    one). ``"name"`` restricts membership to node TITLES; ``"name_and_note"``
    keeps the Workflowy-search-parity name+note membership. It ALSO governs
    Phase 2's ``--expand``/``--suppress``/``--deprecated`` OPERATION
    CLASSIFICATION (084 ruling: annotate passes it into classify_node_tags --
    under ``"name"`` a family postfix counts only in the name's TRAILING tag
    region; under ``"name_and_note"`` it also counts in the note, the prior
    parity behavior).

    ``global_never_expand_tags`` is likewise threaded into BOTH Phase 1-adjacent
    classification (Phase 2, over the minimal tree) AND Phase 3's EXPAND
    hydration walk (the HYDSUP seal): Phase 2 only sees the Phase-1 minimal
    tree, so a globally-suppressed node OUTSIDE it would otherwise be
    re-grafted from the raw cache under an ``--expand`` ancestor. The global
    tag reads the name's TRAILING tag region under EVERY scope (084 D1). See
    apply_expansion_plan_to_subtree's global_never_expand_tags docstring.
    """
    subtree = build_slice_subtree_from_cache(
        cache_by_id,
        root_uuid,
        active_prefixes,
        deprecated_postfix=deprecated_postfix,
        auto_expand_postfix=auto_expand_postfix,
        suppress_postfix=suppress_postfix,
        global_never_expand_tags=global_never_expand_tags,
        exclude_completed=exclude_completed,
        match_scope=match_scope,
        boundary_predicate=boundary_predicate,
    )
    if subtree is None:
        return None, None, {"pruned_zombie_count": 0}

    plan = build_expansion_plan(
        subtree,
        active_prefixes,
        auto_expand_postfix=auto_expand_postfix,
        suppress_postfix=suppress_postfix,
        deprecated_postfix=deprecated_postfix,
        global_never_expand_tags=global_never_expand_tags,
        match_scope=match_scope,
    )
    # exclude_completed threads into Phase 3 too (NOT just Phase 1): the EXPAND
    # re-hydration reads the RAW cache, so an --expand ancestor of a completed
    # subtree would re-inflate it here unless the same prune is applied. See
    # apply_expansion_plan_to_subtree's exclude_completed docstring.
    # global_never_expand_tags threads into Phase 3 for the SAME reason
    # (HYDSUP): a globally-suppressed node absent from the Phase-1 minimal tree
    # would otherwise be re-grafted from the raw cache under an --expand
    # ancestor (the hydration leak this seal closes).
    applied = apply_expansion_plan_to_subtree(
        subtree,
        plan,
        cache_by_id,
        exclude_completed=exclude_completed,
        boundary_predicate=boundary_predicate,
        boundary_root_uuid=root_uuid,
        global_never_expand_tags=global_never_expand_tags,
    )
    phase4 = prune_collapsed_leaf_zombies(applied, plan.get("pending_collapse_ids") or [])
    return (
        phase4["subtree"],
        plan,
        {"pruned_zombie_count": phase4["pruned_zombie_count"]},
    )


# ===========================================================================
# Inline self-test scaffold (run: python -m workflowy_mcp.f8_slice_policy)
# ===========================================================================

def _n(nid: str, name: str = "", children: list[dict[str, Any]] | None = None, note: str | None = None) -> dict[str, Any]:
    return {"id": nid, "name": name, "note": note, "children": children or []}


def _selftest() -> None:
    P = ["tag--a"]

    # --- tag primitives ---
    assert tag_matches_prefix("tag--a", P)
    assert tag_matches_prefix("tag--a--expand", P)          # auto-expand rides bare prefix
    assert tag_matches_prefix("tag--a--deprecated", P)      # literal startsWith
    assert not tag_matches_prefix("other", P)
    f = classify_node_tags("Foo #tag--a", P)
    assert f["has_active"] and not f["has_deprecated"] and not f["has_auto"]
    f = classify_node_tags("Foo #tag--a--deprecated", P)
    assert f["has_active"] and f["has_deprecated"]
    f = classify_node_tags("Foo #tag--a--expand", P)
    assert f["has_active"] and f["has_auto"]
    f = classify_node_tags("Foo #tag--a--suppress", P)
    assert f["has_active"] and f["has_suppress"]
    f = classify_node_tags("Foo #other--x", P, global_never_expand_tags=["other--x"])
    assert f["has_global_never_expand"] and not f["has_active"]

    # --- per-prefix membership (THE rule) ---
    assert is_slice_member("X #tag--a", P)
    assert not is_slice_member("X #tag--a--deprecated", P)        # only prefix deprecated -> out
    assert not is_slice_member("X (no tags)", P)
    # multi-prefix slice: one live prefix is enough even if another is deprecated.
    MP = ["tag--a", "tag--b"]
    assert is_slice_member("X #tag--a #tag--b--deprecated", MP), "live via tag--a despite tag--b deprecated"
    assert is_slice_member("X #tag--b #tag--a--deprecated", MP), "live via tag--b despite tag--a deprecated"
    assert not is_slice_member("X #tag--a--deprecated #tag--b--deprecated", MP), "all prefixes deprecated -> out"

    # --- cache-name HTML-markup / entity tolerance (DOM-vs-cache parity) ---
    # The /nodes-export cache stores tag pills wrapped in presentational markup
    # (e.g. ``#<u>tag--a</u>``) and/or HTML-entity-encoded; the live F8 path
    # never sees this because it re-harvests clean data-val tokens. Membership
    # MUST be identical for the markup-wrapped form or cache slice-group GLIMPSE
    # silently drops members (the bug this guards).
    assert extract_name_tags("X #<u>tag--a</u>") == ["tag--a"], "markup-wrapped tag must canonicalize"
    assert is_slice_member("X #<u>tag--a</u>", P), "markup-wrapped member must be admitted"
    assert is_slice_member("Hdr #<u>tag--a</u> #<u>tag--b</u>", MP), "multiple markup-wrapped tags"
    assert not is_slice_member("X #<u>tag--a--deprecated</u>", P), "markup-wrapped deprecated still excluded"
    assert classify_node_tags("X #<u>tag--a--expand</u>", P)["has_auto"], "markup-wrapped auto-expand detected"
    assert extract_name_tags("a < 5 and b") == [], "a genuine '<' comparison is not a tag"
    # Twin-with-canonical coverage: malformed <-u> interim close marker +
    # entity-encoded forms must canonicalize identically (superset contract).
    assert _strip_tag_markup("#<u>tag--a<-u>") == "#tag--a", "malformed <-u> close marker stripped"
    assert _strip_tag_markup("#&lt;u&gt;tag--a&lt;/u&gt;") == "#tag--a", "entity-encoded <u> stripped"
    assert is_slice_member("X #&lt;u&gt;tag--a&lt;/u&gt;", P), "entity-encoded markup member admitted"

    # --- Workflowy SEARCH substring semantics: tag anywhere in NAME or NOTE ---
    # Confirmed live (Dan): Workflowy matches a tag as a raw substring anywhere
    # in the name OR the note, with NO whitespace boundary required.
    assert extract_tags_from_text("word#tag--a and more") == ["tag--a"], "no-boundary substring tag in text"
    assert extract_tags_from_text("TestD#tag--a nd more") == ["tag--a"], "tag glued mid-word still found"
    assert extract_node_tags("plain name", "body has #tag--a inside") == ["tag--a"], "note-only tag harvested"
    assert extract_node_tags("N #tag--a", "and #tag--b in note") == ["tag--a", "tag--b"], "name+note union"
    # Membership via a NOTE-only tag (name carries none):
    assert is_slice_member("plain name", P, note="see #tag--a here"), "note-only membership admitted"
    assert not is_slice_member("plain name", P, note="see #tag--a--deprecated here"), "note-only deprecated excluded"
    # Classification (suppress/expand) via a NOTE-only tag:
    assert classify_node_tags("plain", P, note="#tag--a--suppress")["has_suppress"], "note-only suppress detected"
    assert classify_node_tags("plain", P, note="#tag--a--expand")["has_auto"], "note-only auto-expand detected"
    # Phase 1 admits a node whose tag lives ONLY in its note.
    _ncache = {
        "root": {"id": "root", "name": "Root", "note": None, "parent_id": None, "priority": 0},
        "n": {"id": "n", "name": "Note-tagged", "note": "prose with #tag--a inside", "parent_id": "root", "priority": 1},
    }
    _nsub = build_slice_subtree_from_cache(_ncache, "root", ["tag--a"])
    _nids = set()
    def _ngather(nn):
        _nids.add(nn["id"])
        for ch in nn["children"]:
            _ngather(ch)
    _ngather(_nsub)
    assert _nids == {"root", "n"}, _nids

    # --- MIDDLE-SEGMENT WILDCARD MATCHER (Dan, July 2026) ---
    # The single shared recognizer: P [ '-<middle>' ] [ '--<postfix>' ].
    PW = _recognized_postfix_words()  # {expand, suppress, deprecated}
    # bare prefix -> match, empty middle, no postfix.
    m = slice_tag_variant_match("comm-jim", "comm-jim", postfix_words=PW)
    assert m == {"matched_prefix": "comm-jim", "middle": "", "postfix": ""}, m
    # prefix + '--deprecated' (no middle).
    m = slice_tag_variant_match("comm-jim--deprecated", "comm-jim", postfix_words=PW)
    assert m == {"matched_prefix": "comm-jim", "middle": "", "postfix": "deprecated"}, m
    # THE BUG CASE: prefix + '-teams' MIDDLE + '--deprecated'.
    m = slice_tag_variant_match("comm-jim-teams--deprecated", "comm-jim", postfix_words=PW)
    assert m == {"matched_prefix": "comm-jim", "middle": "-teams", "postfix": "deprecated"}, m
    # prefix + '-teams' middle, NO postfix -> plain family member.
    m = slice_tag_variant_match("comm-jim-teams", "comm-jim", postfix_words=PW)
    assert m == {"matched_prefix": "comm-jim", "middle": "-teams", "postfix": ""}, m
    # middle + '--expand' / '--suppress'.
    assert slice_tag_variant_match("comm-jim-teams--expand", "comm-jim", postfix_words=PW)["postfix"] == "expand"
    assert slice_tag_variant_match("comm-jim-teams--suppress", "comm-jim", postfix_words=PW)["postfix"] == "suppress"
    # BOUNDARY RULE: a middle NOT starting with '-' does NOT match (no bleed
    # into a sibling family). This is the Q1 ruling (hyphen-delimited middle).
    assert slice_tag_variant_match("comm-jimx", "comm-jim", postfix_words=PW) is None, "comm-jimx must NOT match comm-jim"
    assert slice_tag_variant_match("comm-jimmy", "comm-jim", postfix_words=PW) is None, "comm-jimmy must NOT match comm-jim"
    assert slice_tag_variant_match("comm-jimx--deprecated", "comm-jim", postfix_words=PW) is None, "comm-jimx--deprecated must NOT match comm-jim"
    # non-prefix tag -> None.
    assert slice_tag_variant_match("other--x", "comm-jim", postfix_words=PW) is None

    # --- MIDDLE-SEGMENT membership + classification (the real fix) ---
    JP = ["comm-jim"]
    # A node whose ONLY comm-jim tag is a middle-segment DEPRECATED variant is
    # NOT a member (deprecated on its only prefix) -- previously it was WRONGLY
    # live (naked startswith saw comm-jim but missed the middle-segment
    # deprecation). THIS is the bug being fixed.
    assert not is_slice_member("X #comm-jim-teams--deprecated", JP), "middle-seg deprecated must evict"
    # A node with a plain middle-segment variant (no postfix) IS a member.
    assert is_slice_member("X #comm-jim-teams", JP), "middle-seg plain variant is a member (same family)"
    # comm-jimx (no hyphen boundary) does NOT join the comm-jim slice.
    assert not is_slice_member("X #comm-jimx", JP), "comm-jimx is a different family, not a member"
    # classify: the middle-seg deprecated variant sets has_active + has_deprecated.
    cf = classify_node_tags("X #comm-jim-teams--deprecated", JP)
    assert cf["has_active"] and cf["has_deprecated"], cf
    # classify: the middle-seg plain variant sets has_active only.
    cf = classify_node_tags("X #comm-jim-teams", JP)
    assert cf["has_active"] and not cf["has_deprecated"] and not cf["has_auto"] and not cf["has_suppress"], cf
    # classify: middle-seg --expand / --suppress variants set the right flag.
    assert classify_node_tags("X #comm-jim-teams--expand", JP)["has_auto"], "middle-seg expand"
    assert classify_node_tags("X #comm-jim-teams--suppress", JP)["has_suppress"], "middle-seg suppress"
    # WITHIN-PREFIX DEPRECATION (the three-way-equivalence anchor): all middle-
    # segment variants of P bind to the SAME base prefix P, so ANY deprecated
    # variant of P (bare OR middle-segment) deprecates P on this node -- exactly
    # what Workflowy's compiled search `#comm-jim -#comm-jim--deprecated
    # -#comm-jim-teams--deprecated` computes (the negation excludes the node if
    # it carries EITHER deprecated tag, regardless of a co-present plain
    # variant). So a node carrying both a plain middle variant AND a deprecated
    # variant of the SAME base prefix is NOT live via that prefix.
    assert not is_slice_member("X #comm-jim-teams #comm-jim--deprecated", JP), "a deprecated variant of the base prefix evicts, even with a co-present plain variant (Workflowy-search-equivalent)"
    assert not is_slice_member("X #comm-jim #comm-jim-teams--deprecated", JP), "a middle-seg deprecated variant also evicts the base prefix (search-equivalent)"
    # CROSS-PREFIX OR is unchanged: a DIFFERENT live active prefix keeps the
    # node in even though comm-jim is fully deprecated.
    MJ = ["comm-jim", "topic-conversational"]
    assert is_slice_member("X #comm-jim--deprecated #topic-conversational", MJ), "live via a different active prefix (per-prefix OR)"

    # --- compiled search string (BUNDLE return; per-prefix pairing, no parens) ---
    r = compile_slice_search_expression(["tag--a"])
    assert r["search_string"] == "#tag--a -#tag--a--deprecated" and not r["too_many_variants"], r
    r = compile_slice_search_expression(["tag--a", "tag--b"])
    assert (
        r["search_string"]
        == "#tag--a -#tag--a--deprecated OR #tag--b -#tag--b--deprecated"
    ), r
    # WITH discovered middle-segment deprecated variants: each is negated after
    # its prefix's positive term (the explosion, enumerated explicitly).
    r = compile_slice_search_expression(
        ["comm-jim"],
        deprecated_variants_by_prefix={"comm-jim": ["comm-jim--deprecated", "comm-jim-teams--deprecated", "comm-jim-phone--deprecated"]},
    )
    assert r["search_string"] == (
        "#comm-jim -#comm-jim--deprecated -#comm-jim-teams--deprecated -#comm-jim-phone--deprecated"
    ), r
    assert not r["too_many_variants"] and r["term_count"] == 4, r
    # CAP: exceed max_terms -> REFUSE (search_string None, too_many_variants).
    many = [f"comm-jim-v{i}--deprecated" for i in range(200)]
    r = compile_slice_search_expression(
        ["comm-jim"],
        deprecated_variants_by_prefix={"comm-jim": many},
        max_terms=80,
    )
    assert r["search_string"] is None and r["too_many_variants"] and r["term_count"] > 80, r

    # --- collect_deprecated_variant_tags: scan the cache for dep variants ---
    _vcache = {
        "root": {"id": "root", "name": "Root", "note": None, "parent_id": None, "priority": 0},
        "a": {"id": "a", "name": "A #comm-jim", "note": None, "parent_id": "root", "priority": 1},
        "b": {"id": "b", "name": "B #comm-jim-teams--deprecated", "note": None, "parent_id": "root", "priority": 2},
        "c": {"id": "c", "name": "C #comm-jim--deprecated", "note": None, "parent_id": "root", "priority": 3},
        "d": {"id": "d", "name": "D #comm-jimx--deprecated", "note": None, "parent_id": "root", "priority": 4},
    }
    dv = collect_deprecated_variant_tags(_vcache, "root", ["comm-jim"])
    # b + c match (comm-jim family, deprecated); d (comm-jimx) is a DIFFERENT
    # family and must NOT be collected.
    assert dv == {"comm-jim": ["comm-jim--deprecated", "comm-jim-teams--deprecated"]}, dv

    # --- Phase 1: search + minimal subtree from cache ---
    cache = {
        "root": {"id": "root", "name": "Root", "note": None, "parent_id": None, "priority": 0},
        "a": {"id": "a", "name": "A #tag--a", "note": None, "parent_id": "root", "priority": 1},
        "b": {"id": "b", "name": "B (no tag)", "note": None, "parent_id": "root", "priority": 2},
        "b1": {"id": "b1", "name": "B1 #tag--a", "note": None, "parent_id": "b", "priority": 1},
        "c": {"id": "c", "name": "C #tag--a--deprecated", "note": None, "parent_id": "root", "priority": 3},
    }
    # Add a per-prefix node: live via tag--a even though tag--b is deprecated.
    cache["d"] = {"id": "d", "name": "D #tag--a #tag--b--deprecated", "note": None, "parent_id": "root", "priority": 4}
    sub = build_slice_subtree_from_cache(cache, "root", ["tag--a", "tag--b"])
    ids = set()

    def gather(n):
        ids.add(n["id"])
        for ch in n["children"]:
            gather(ch)
    gather(sub)
    # a (hit), b (ancestor of b1), b1 (hit), d (live via tag--a), root kept;
    # c (deprecated on its only prefix) excluded.
    assert ids == {"root", "a", "b", "b1", "d"}, ids

    # --- Phase 0: strict validation ---
    dom_ok = _n("root", "Root", [_n("a", "A #tag--a")])
    cache2 = {
        "root": {"id": "root", "name": "Root", "parent_id": None},
        "a": {"id": "a", "name": "A #tag--a", "parent_id": "root"},
    }
    assert validate_subtree_against_cache(dom_ok, cache2)["ok"]
    # Genuinely-missing UUID (cache has not ingested it) -> fail-closed mismatch.
    dom_bad = _n("root", "Root", [_n("ghost", "missing")])
    bad = validate_subtree_against_cache(dom_bad, cache2)
    assert not bad["ok"] and bad["error"] == "visible_tree_cache_mismatch", bad
    assert bad["details"]["missing"] == ["ghost"], bad
    # MEMBERSHIP-ONLY contract (the overzealous-edge-check fix): a node whose
    # DOM parent differs from the cache parent_id is NOT a mismatch as long as
    # its UUID exists in cache. The strict parent->child edge comparison was
    # removed (it produced constant false mismatches at the zoom-root / virtual-
    # node boundary, exactly the docked-modal lesson). So this 'moved' tree,
    # where every UUID is present, must validate OK.
    dom_moved = _n("root", "Root", [_n("a", "A", [_n("x", "X")])])
    cache3 = {
        "root": {"id": "root", "parent_id": None},
        "a": {"id": "a", "parent_id": "root"},
        "x": {"id": "x", "parent_id": "root"},  # cache says x under root, DOM says under a
    }
    assert validate_subtree_against_cache(dom_moved, cache3)["ok"], "edge moves are NOT staleness (UUID-membership-only)"

    # --- Phase 2: plan ---
    # suppress dominates a subtree; auto-expand elsewhere; deprecated-with-live-child not collapsed.
    tree = _n("root", "Root #tag--a", [
        _n("s", "Suppressed #tag--a--suppress", [_n("s1", "hidden #tag--a")]),
        _n("e", "AutoExpand #tag--a--expand", [_n("e1", "child #tag--a")]),
        _n("d", "Deprecated-with-live #tag--a--deprecated", [_n("d1", "live #tag--a")]),
        _n("d2", "Deprecated-pure #tag--a--deprecated", [_n("d2a", "no tag")]),
    ])
    plan = build_expansion_plan(tree, P)
    assert plan["collapse_suppressed"] == ["s"], plan
    assert "e" in plan["expand"], plan
    # d has a live child -> NOT collapsed; d2 is pure-deprecated -> collapsed
    assert plan["collapse_deprecated"] == ["d2"], plan

    # auto-expand ancestor over a suppressed descendant: expand wins, suppress dropped from under it.
    tree2 = _n("root", "Root #tag--a", [
        _n("e", "AutoExpand #tag--a--expand", [
            _n("s", "Suppressed-under-expand #tag--a--suppress", [_n("s1", "x #tag--a")]),
        ]),
    ])
    plan2 = build_expansion_plan(tree2, P)
    assert "e" in plan2["expand"], plan2
    # s is still a collapse target (it's an ancestor of nothing expanded); expand 'e' runs last and dominates.
    assert plan2["collapse_suppressed"] == ["s"], plan2

    # --- EMPTY-ID "Home" ROOT (search-mode) is TRANSPARENT ---
    # Under an active tag search Workflowy roots the rendered project at the
    # synthetic "Home" node (empty projectid, absent from cache). The empty
    # root must be skipped but its children walked normally, so validation,
    # the plan, and the diagnostic all behave as if the real top-level node
    # were the root.
    home = _n("", "Home", [
        _n("top", "Workflowy Root #tag--a", [
            _n("x", "AutoExpand #tag--a--expand", [_n("x1", "child #tag--a")]),
        ]),
    ])
    home_cache = {
        "top": {"id": "top", "name": "Workflowy Root #tag--a", "parent_id": None},
        "x": {"id": "x", "name": "AutoExpand #tag--a--expand", "parent_id": "top"},
        "x1": {"id": "x1", "name": "child #tag--a", "parent_id": "x"},
    }
    # Validation passes: every REAL (non-empty-id) uuid exists in cache; the
    # empty Home root is transparent and not checked.
    assert validate_subtree_against_cache(home, home_cache)["ok"], "empty Home root must be transparent"
    # Edge diagnostic: 'top' is now a (transparent-root) child with dom_parent
    # None, matching cache parent_id None -> NO edge mismatch anywhere.
    diag = diagnose_subtree_edges(home, home_cache)
    assert diag["edge_mismatches"] == [], diag
    assert diag["missing"] == [], diag
    # Plan: the #tag--a--expand node 'x' is a real auto-expand target (NOT the
    # discarded root), so it appears in expand.
    home_plan = build_expansion_plan(home, P)
    assert "x" in home_plan["expand"], home_plan

    # SENTINEL ID: the real DOM delivers Home's projectid as the literal STRING
    # "None" (not empty). _node_id must normalize "None"/"null"/"undefined" to ''
    # so the same transparency applies. Re-run the Home scenario with id="None".
    home_sentinel = _n("None", "Home", [
        _n("top", "Workflowy Root #tag--a", [
            _n("x", "AutoExpand #tag--a--expand", [_n("x1", "child #tag--a")]),
        ]),
    ])
    assert _node_id(home_sentinel) == "", "literal 'None' id must normalize to ''"
    assert validate_subtree_against_cache(home_sentinel, home_cache)["ok"], "'None'-id Home must be transparent"
    assert diagnose_subtree_edges(home_sentinel, home_cache)["edge_mismatches"] == []
    assert "x" in build_expansion_plan(home_sentinel, P)["expand"]

    # --- normalize_slice_group_tags (agent-supplied tag list) ---
    # Strips '#', collapses each of the three live postfix variants to base,
    # de-dupes order-preserving, drops malformed.
    r = normalize_slice_group_tags(
        "#foo, bar--expand , baz--suppress, foo, qux--deprecated, #bar",
        auto_expand_postfix="expand",
        suppress_postfix="suppress",
        deprecated_postfix="deprecated",
    )
    assert r["bases"] == ["foo", "bar", "baz", "qux"], r
    # bar appeared as bar--expand then #bar -> single 'bar'; foo twice -> once.
    collapsed_inputs = {c["input"] for c in r["collapsed"]}
    assert collapsed_inputs == {"bar--expand", "baz--suppress", "qux--deprecated"}, r
    # list input works identically.
    assert normalize_slice_group_tags(["a", "a--expand", "b"])["bases"] == ["a", "b"]
    # malformed: bare postfix (empty base) and trailing-sep base are dropped.
    rm = normalize_slice_group_tags(["--expand", "foo----expand", "ok"])
    assert rm["bases"] == ["ok"], rm
    assert "--expand" in rm["dropped"] and "foo----expand" in rm["dropped"], rm
    # internal whitespace / embedded '#' dropped; non-postfix 'fooexpand' kept.
    rw = normalize_slice_group_tags(["a b", "c#d", "fooexpand"])
    assert rw["bases"] == ["fooexpand"], rw
    # custom postfix words honored (server passes live ones).
    rc = normalize_slice_group_tags(["t--grow"], auto_expand_postfix="grow")
    assert rc["bases"] == ["t"], rc

    # --- PHASE 4: prune_collapsed_leaf_zombies (render-only zombie prune) ---
    def _ids_in(node):
        got = set()
        def _w(n):
            nid = _node_id(n)
            if nid:
                got.add(nid)
            for ch in n.get("children") or []:
                _w(ch)
        _w(node)
        return got

    # (1) Plain zombie prune: a childless deprecated/suppressed contraction
    #     leaf (as Phase 3 leaves it) is REMOVED from the render tree; live
    #     siblings survive. Input is the ALREADY-collapsed (post-Phase-3) shape:
    #     the contraction nodes are already childless leaves.
    post_p3 = _n("root", "Root", [
        _n("live", "Live member #tag--a"),
        _n("dep", "Deprecated leaf #tag--a--deprecated"),   # childless zombie
        _n("sup", "Suppressed leaf #tag--a--suppress"),     # childless zombie
    ])
    res = prune_collapsed_leaf_zombies(post_p3, ["dep", "sup"])
    assert res["pruned_zombie_count"] == 2, res
    assert _ids_in(res["subtree"]) == {"root", "live"}, _ids_in(res["subtree"])
    # input untouched (new tree returned).
    assert _ids_in(post_p3) == {"root", "live", "dep", "sup"}, "input must not mutate"

    # (2) Promoted-blank-ancestor zombie: Phase 2's dead-limb promotion can put
    #     a BLANK structural ancestor (no tag) into the contraction set. It is
    #     still a childless collapse root post-Phase-3 and is pruned identically
    #     (Phase 4 keys off the set, NOT off tags).
    post_p3b = _n("root", "Root", [
        _n("keep", "Live #tag--a"),
        _n("blank", "Blank promoted ancestor (no tag)"),  # childless, in set
    ])
    resb = prune_collapsed_leaf_zombies(post_p3b, ["blank"])
    assert resb["pruned_zombie_count"] == 1, resb
    assert _ids_in(resb["subtree"]) == {"root", "keep"}, _ids_in(resb["subtree"])

    # (3) All-clean no-op: empty contraction set -> tree unchanged, count 0.
    clean = _n("root", "Root", [_n("a", "A #tag--a"), _n("b", "B #tag--a")])
    resc = prune_collapsed_leaf_zombies(clean, [])
    assert resc["pruned_zombie_count"] == 0, resc
    assert _ids_in(resc["subtree"]) == {"root", "a", "b"}, resc

    # (4) ABSENT contraction UUID is skipped (NOT an error): a collapse target
    #     nested under another collapse target was removed with that target's
    #     descendants, so it never appears in the tree.
    resd = prune_collapsed_leaf_zombies(clean, ["ghost-not-in-tree"])
    assert resd["pruned_zombie_count"] == 0, resd
    assert _ids_in(resd["subtree"]) == {"root", "a", "b"}, resd

    # (5) LOGIC-ERROR (fail loud): a contraction node PRESENT with surviving
    #     children is an upstream invariant break -> SliceProjectionInvariantError.
    broken = _n("root", "Root", [
        _n("dep", "Deprecated WITH children #tag--a--deprecated", [
            _n("child", "should not exist under a collapsed node"),
        ]),
    ])
    try:
        prune_collapsed_leaf_zombies(broken, ["dep"])
        raise AssertionError("expected SliceProjectionInvariantError (contraction node has children)")
    except SliceProjectionInvariantError:
        pass

    # (6) LOGIC-ERROR (fail loud): the render ROOT must never be a contraction
    #     target (Phase 2 discards root). If it is, refuse rather than nuke all.
    try:
        prune_collapsed_leaf_zombies(clean, ["root"])
        raise AssertionError("expected SliceProjectionInvariantError (root in contraction set)")
    except SliceProjectionInvariantError:
        pass

    # --- COMPLETED-NODE EXCLUSION (server-side GLIMPSE Phase-1 admit prune) ---
    # Exercises build_slice_subtree_from_cache(exclude_completed=...) directly
    # over a flat cache map (the actual filter site). The cache node carries a
    # boolean `completed` (and companion `completedAt`), exactly like
    # /nodes-export.
    def _cn(nid, name, parent, *, completed=False, completed_at=None, priority=0):
        return {
            "id": nid, "name": name, "parent_id": parent,
            "priority": priority, "note": None,
            "completed": completed, "completedAt": completed_at,
        }

    def _sub_ids(root):
        got = set()
        def _w(n):
            nid = _node_id(n)
            if nid:
                got.add(nid)
            for ch in n.get("children") or []:
                _w(ch)
        if root is not None:
            _w(root)
        return got

    # Cache shape:
    #   root
    #     m1                (member #tag--a)
    #     done              (COMPLETED, carries #tag--a--expand)
    #       done-kid        (member #tag--a, but hidden by completed ancestor)
    #     m2                (member #tag--a)
    CM = {n["id"]: n for n in [
        _cn("root", "Root", None, priority=0),
        _cn("m1", "M1 #tag--a", "root", priority=100),
        _cn("done", "Done branch #tag--a--expand", "root", priority=200, completed=True, completed_at=123),
        _cn("done-kid", "Under done #tag--a", "done", priority=100),
        _cn("m2", "M2 #tag--a", "root", priority=300),
    ]}

    # (A) exclude_completed=True: the completed `done` branch is pruned at admit
    #     time -- neither `done` nor `done-kid` appears, and the --expand
    #     postfix on `done` CANNOT resurrect it (it is gone before Phase 2).
    subA = build_slice_subtree_from_cache(CM, "root", ["tag--a"], exclude_completed=True)
    assert _sub_ids(subA) == {"root", "m1", "m2"}, _sub_ids(subA)

    # (B) DEFAULT (exclude_completed=False): the completed branch is admitted
    #     normally (both `done` and `done-kid` are members, so both are kept as
    #     connective+member nodes).
    subB = build_slice_subtree_from_cache(CM, "root", ["tag--a"])
    assert _sub_ids(subB) == {"root", "m1", "done", "done-kid", "m2"}, _sub_ids(subB)

    # (C) COMPLETED ROOT is exempt by construction: targeting the completed
    #     `done` node AS the root still shows its (non-completed) subtree (the
    #     branch-free root-inclusion rule).
    subC = build_slice_subtree_from_cache(CM, "done", ["tag--a"], exclude_completed=True)
    assert _sub_ids(subC) == {"done", "done-kid"}, _sub_ids(subC)

    # (D) RAW DUMP (member_all=True) composes with exclude_completed: every
    #     descendant EXCEPT the completed subtree. `done`/`done-kid` gone; all
    #     other nodes kept regardless of tags.
    subD = build_slice_subtree_from_cache(CM, "root", [], member_all=True, exclude_completed=True)
    assert _sub_ids(subD) == {"root", "m1", "m2"}, _sub_ids(subD)
    #     ...and a raw dump WITHOUT the policy keeps everything (default off).
    subD0 = build_slice_subtree_from_cache(CM, "root", [], member_all=True)
    assert _sub_ids(subD0) == {"root", "m1", "done", "done-kid", "m2"}, _sub_ids(subD0)

    # (E) `completedAt`-only signal (completed flag absent/False but completedAt
    #     populated) still reads as completed -> pruned.
    CM_at = {n["id"]: n for n in [
        _cn("root", "Root", None, priority=0),
        _cn("k", "Keep #tag--a", "root", priority=100),
        _cn("c", "CompletedAt-only #tag--a", "root", priority=200, completed=False, completed_at=999),
    ]}
    subE = build_slice_subtree_from_cache(CM_at, "root", ["tag--a"], exclude_completed=True)
    assert _sub_ids(subE) == {"root", "k"}, _sub_ids(subE)

    # (F) _node_is_completed unit checks (defensive fail-open on junk input).
    assert _node_is_completed({"completed": True})
    assert _node_is_completed({"completedAt": 1})
    assert not _node_is_completed({"completed": False, "completedAt": None})
    assert not _node_is_completed({})
    assert not _node_is_completed(None)  # type: ignore[arg-type]

    # (G) THE PHASE-3 RE-HYDRATION CASE (the bug this guard closes). An
    #     --expand node E is an ANCESTOR of a COMPLETED node C (with a live
    #     grandchild gk under C). Phase 1 excludes C; Phase 2 puts E in expand;
    #     Phase 3's EXPAND re-hydrates E's children straight from the RAW cache.
    #     Without the hydration guard, C (and gk) would be re-inflated, undoing
    #     the exclusion. With it, C's whole subtree stays hidden through the
    #     FULL funnel (build_stage1_base_tree). This is the end-to-end
    #     proof that 'no --expand postfix can un-hide a completed subtree'.
    CM_exp = {n["id"]: n for n in [
        _cn("root", "Root", None, priority=0),
        _cn("E", "Expander #tag--a--expand", "root", priority=100),
        _cn("C", "Completed child #tag--a", "E", priority=100, completed=True, completed_at=7),
        _cn("gk", "Grandkid under completed #tag--a", "C", priority=100),
        _cn("liveE", "Live child of E #tag--a", "E", priority=200),
    ]}
    finalG, planG, _mG = build_stage1_base_tree(
        CM_exp, "root", ["tag--a"], exclude_completed=True
    )
    assert planG.get("expand") == ["E"], planG.get("expand")
    idsG = _sub_ids(finalG)
    assert "C" not in idsG and "gk" not in idsG, idsG   # completed subtree hidden...
    assert {"root", "E", "liveE"} <= idsG, idsG         # ...but the expand still worked

    # (H) DEFAULT (policy off) on the SAME shape re-inflates everything through
    #     the expand -- proving the guard, not some other filter, is what hides
    #     the completed subtree (and that default-off is byte-for-byte prior).
    finalH, _pH, _mH = build_stage1_base_tree(CM_exp, "root", ["tag--a"])
    assert {"C", "gk"} <= _sub_ids(finalH), _sub_ids(finalH)

    # (I) COMPLETED --expand node E directly under a (non-completed) root: the
    #     ANCESTOR rule dominates the expand tag. With exclude_completed, Phase
    #     1's collect() will not descend into E (E is a completed CHILD of
    #     root), so E is never admitted, never enters the expand plan, and its
    #     whole branch (clean + completed children alike) is hidden. Proves an
    #     --expand postfix on a completed node cannot rescue that node when a
    #     non-completed ancestor sees it as completed. (The 'expand TARGET is
    #     exempt' symmetry with the root only applies to a target that WAS
    #     admitted -- e.g. a completed root passed as root_uuid, per case C.)
    CM_exp2 = {n["id"]: n for n in [
        _cn("root", "Root", None, priority=0),
        _cn("E", "Completed expander #tag--a--expand", "root", priority=100, completed=True, completed_at=3),
        _cn("ck", "Clean child of E #tag--a", "E", priority=100),
        _cn("dk", "Completed child of E #tag--a", "E", priority=200, completed=True, completed_at=4),
    ]}
    # NB: with exclude_completed, Phase 1 will NOT descend into E from root
    # (E is a completed child of root), so E is not even admitted -> the whole
    # branch is hidden. This asserts the ancestor rule dominates: a completed
    # E under a non-completed root hides E entirely, expand tag notwithstanding.
    finalI, _pI, _mI = build_stage1_base_tree(
        CM_exp2, "root", ["tag--a"], exclude_completed=True
    )
    assert _sub_ids(finalI) == {"root"}, _sub_ids(finalI)

    print("f8_slice_policy self-test: ALL PASS")


if __name__ == "__main__":
    _selftest()
