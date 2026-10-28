# Scoped Lightning Rod

The **[Scoped | Global]** toggle on the dock's ⚡ Lightning Rod ribbon confines a
Lightning Rod (`gather` / `render_tags` / `render_search`) to the dock's
**current frozen artifact** instead of walking the whole `/nodes-export` cache
from the modal root.

- **Global** (default): the Lightning Rod searches the entire cache subtree
  under the modal's current root — the original behavior.
- **Scoped**: the search is confined to what the dock is **currently showing**
  (a frozen DOM view or an applied slice), optionally narrowed to the
  **selected row's** subtree first.

Scoped is only meaningful when the dock is **frozen** (an applied slice is
frozen by construction; a DOM view must be explicitly frozen). The toggle is
greyed / forced to Global otherwise.

## What a Scoped run produces (Option B — the one-way ephemeral collapse)

A Scoped Lightning Rod **always** produces an **ephemeral** `kind='dom_capture'`
artifact — for **both** sources:

- **DOM-frozen**: the pinned captured forest is filtered directly.
- **Slice-frozen**: the durable slice is **re-projected from the cache**
  (via the one shared `build_stage1_base_tree` funnel), then filtered.
  Running a Lightning Rod on an applied slice **collapses** that slice into a
  frozen in-memory view of the results — it will **not** pick up Workflowy edits
  on ↻ Refresh. A yellow bar says so; **re-apply the slice** to return to the
  durable curate-and-refresh loop. (The durable slice still exists; you just
  re-Apply it.)

"Lightning-within-Lightning" is free: a second Scoped run filters the previous
result (wholesale forest replacement, no history/stack). Start over by
re-Applying the slice.

The ephemeral `dom_capture` artifact is **never** laundered back into a durable
`RenderProjectionSpec` (the "crack" the provenance adapter closes at one choke
point: `displaySliceInDock` → `displayScopedDomCaptureInDock`).

## Row re-root (occurrence-exact)

When a row is selected and Scoped is fired, the domain is that row's
occurrence-exact subtree: the artifact is sorted, the clicked
`(uuid, ancestor_path, occurrence)` triple is resolved **exact-or-stale**
(a mismatch → a loud "view is out of sync; re-freeze / re-apply" error, never a
silently wrong render), pruned to that occurrence, and rebuilt de-mirrored from
the canonical (unsorted) tree before filtering. Plain row-click **navigation**
is untouched — only a *fired* Scoped Lightning re-roots.

## v1 constraints (deliberate non-goals — please don't file as bugs)

- **Single real root only.** Every artifact (slice group, DOM modal, scoped
  result) is rooted at exactly **one real, non-empty UUID**. Workflowy's "home"
  is a UI affordance, not a clickable node; an empty-UUID "home" root is never a
  valid render/scope root at any layer. A `>1`-root or empty-id-root artifact is
  impossible by construction and **raises loudly** (never a silent synthetic
  container). A slice group *may* select members spanning multiple top-level
  nodes via tags, but the group itself is still rooted at one real node.
- **Workflowy mirrors are unsupported.** The native mirror feature (one UUID in
  2+ places) is a v1 non-goal; the code assumes a UUID appears **at most once**
  in the raw (unsorted) tree.
- **`render_symbols` is not a Scoped-DOM mode.** It is inherently a cache
  symbol→UUID resolution mode over the whole vault, so it is rejected over a
  frozen DOM view (use it as a Global Lightning Rod).
- **`since='curated'` is not supported for a Scoped run** (a Lightning Rod has
  no persisted slice tag set to define "curated"); time-window (`1w`) and
  item-count (`last N`) `since` are supported.

These are the v2 chasm; they are documented so nobody re-audits them as cracks.
