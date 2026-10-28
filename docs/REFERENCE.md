*Formerly the top-level README: feature notes and release notes. Paths are relative to the repository root; the front page is now [README.md](../README.md).*

# workflowy-nexus-mcp-tools

Private staging repo for NEXUS Core v1 (Workflowy MCP server + Cartographer/GLIMPSE integration).

Initial stub; layout and code will be migrated here from existing working trees.

## Dock peek: source metrics and optional recursive inventory

Use the `anchor` family:

```json
{"verb":"dock.peek","params":{"dock_handle":"[ap:ROOT]"}}
{"verb":"dock.peek","params":{"dock_handle":"[ap:ROOT]","recursive":true}}
{"verb":"dock.peek","params":{"dock_handle":"[ap:ROOT]","recursive":true,"descend_policy":"all"}}
```

**Direct children remain the default.** `recursive=true` lists descendants in
actual node hierarchy/document order. It is not the compressed Dock-only
ancestry used by `dock.list`: ordinary nodes remain visible and contribute
to depth. By default, nested Docks are **marked leaf signposts, not entered**.
With recursion enabled, `descend_policy="all"` explicitly enters nested Docks,
global and local alike. The default is `"stop_at_child_anchors"`, using the
same vocabulary as GLIMPSE. This is a local gate on the existing peek walk,
not a new traversal or transport path. `descend_policy` never turns recursion
on by itself. There is still no pagination; completed nodes remain included.

The header explains **⚓ Dock**, **Ⓛ local Dock**, and **✓ completed**, and says
whether recursion stops at or enters nested Docks.

**One AP-only column at the far left:** find the maximum AP-ID length across
all displayed rows, regardless of depth or group. Keep each AP-ID intact,
right-pad AFTER its closing bracket to that width, add one space, then indent
the trimmed body by four spaces per node level. Missing AP-IDs get whitespace,
never a UUID substitute. If no displayed rows have AP-IDs, column width is
zero and there is no separator or phantom leading padding. Hidden descendants'
AP-IDs do not affect the width.

`show_ids=true` adds a bracketed 8-character UUID prefix to the **indented
body**, while AP-IDs stay in the left column and are removed from the body.
Prefix collisions retain the existing resolver's loud disambiguation behavior.

Rows and the Dock header share four compact fields:

```text
▸3 · desc:27 · self:1.4KiB · tree:86.0KiB
```

- `▸`: all direct children.
- `desc`: all descendants, excluding the node itself.
- `self`: complete semantic **title + note** UTF-8 size.
- `tree`: that node's own size plus all descendant title/note sizes.

Totals describe the **full underlying operation snapshot**, including
completed/suppressed nodes and content behind nested Dock signposts. They are
not a count/size of the displayed rows and are not live verification. AP-IDs,
tags, Markdown and AST/beacon metadata count; nothing is stripped for these
sizes. JSON overhead, generated Markdown headings and invented field
separators do not count. Small sizes use bytes (a tiny nonempty node never
rounds to zero); larger sizes use KiB/MiB/etc., with 1 KiB = 1024 bytes.

`show_counts=false` hides all four metrics in compact mode. Description
excerpts and title ellipsis remain presentation-only; truncating a title does
not reduce its measured size. `detail='full'` returns the existing row fields
plus `depth` (direct children = 0), `completed`, `descendant_count`, `self_bytes`
and `subtree_bytes`, with top-level `recursive`, `descend_policy` and four-field
`root_metrics`.

The existing one config read, handle resolver and pinned operation-cache
boundary are unchanged. A pure helper creates owned ID lists and numeric
records, reuses the existing direct-child count authority, and aggregates the
requested source subtree in iterative postorder. No per-row subtree recount,
per-node live GET, source-node mutation, cache publication, persistent metrics
cache, governor/claim change, or browser transport change. Headings-only and
Dock rendering are intentionally untouched.

## One full server per account

Every full HTTP or stdio server takes one nonblocking OS writer lock before
client, recovery, broker connection or service work and keeps it until process
death. Its sole path is `<jobs_root>/.coherence/<32-hex account>/writer.lock`;
the scope uses the existing normalized API URL/key hash, never the HTTP port.
Use the same configured jobs root for all full entries of an account. Moving
that root is an offline relocation of the whole artifact set. A regenerated
API key produces a different scope; there is no cross-key account discovery.
The file may remain after exit: ownership is the OS lock, not file existence.
Do not delete it or run a second full server on another port.

```text
FATAL: another Workflowy full server already holds '{lock_path}'. This process will not serve. Use workflowy-mcp-shim or connect to the existing daemon.
FATAL: cannot acquire the Workflowy writer guard at '{lock_path}' ({exception_type}). This process will not serve. Check access to this configured jobs directory; do not delete writer.lock.
```

Those entry failures exit 75 (busy) and 74 (unavailable), respectively.
Full servers and the GLIMPSE broker refuse WSL with exit 2: run them on
Windows and use `workflowy-mcp-shim` or an HTTP client from WSL. Native Linux,
macOS and Windows remain supported, including Windows executables launched
from a WSL shell. The test-only override is not a deployment flag and never
skips the writer guard. `workflowy-mcp` with no transport selector runs the stdio shim by default. Existing host command and env blocks need no change. `python -m workflowy_mcp` has the same behavior; `workflowy-mcp-shim` remains an explicit shim entry. Use `--transport stdio` (or `WORKFLOWY_MCP_TRANSPORT=stdio`) for the maintained guarded full stdio server, or `--transport http` (or env `http`) for the daemon. `--transport shim` explicitly selects the default; unknown selector values warn and use the shim.

A newly installed, replacement-capable native shim compares installed identity
in the existing attach negotiation and automatically drains/replaces an older
daemon from that shim's own installation. Admitted work finishes; long bulk
jobs stop only at a safe file-group boundary. The drain bound is at least an
hour and can only be lengthened: expiry cancels the restart attempt, not work,
and the old daemon resumes serving. Older or unorderable shims warn and attach.
An editable/dirty source identity cannot order same-version source releases.
A Linux-process WSL shim, or a shim with different/missing host/OS affinity,
warns and keeps forwarding; it never stops or locally spawns that peer. Use the
Windows-side shim to upgrade a Windows daemon. A running old shim never fetches
latest. There is no in-process full-server fallback and no forwarded-call replay.

During a pause, new calls get typed `restarting`, `outcome:not_sent`, and
`retry_after` (30 seconds while draining, 1 while exiting), with zero handler
effects. An unsuccessful automatic upgrade is a typed `restart_failed` for that
attach/call: after `drain_timeout`, retry the same upgraded shim once active work
finishes; after `control_failed`, retry that attach (the old daemon still serves).
After `exit_timeout` or `spawn_failed`, start the same upgraded installation on
the daemon's native host with `workflowy-mcp --transport http` and check its log;
never delete writer.lock or clear the uv cache as a restart remedy. With no reachable daemon, a native shim dialing loopback keeps its existing cold-start recovery regardless of any previously observed peer. A Linux-process WSL shim or a shim dialing a non-loopback destination returns `daemon_unavailable` and never launches a local full server for that destination. Start the native-host shim there, or use the local loopback destination for a local daemon. Reachable destinations still forward; no DNS or interface discovery is performed.
If a connection ends after forwarding, `upstream_outcome_unknown` is not retryable:
"The connection ended after this call was forwarded; its outcome is unknown and
the shim did not resend it. Read back the affected state before deciding whether
to issue a new call."

## Claude Code large tool results

Every published tool in both registration profiles declares
`_meta["anthropic/maxResultSizeChars"] = 500000` in `tools/list`, preserving
other metadata. This asks Claude Code to deliver large successful results
inline rather than its default file preview. It does not change tool schemas,
execution, results, or the server's existing read guards. Claude Code's native
500,000-character inline ceiling and 16 MiB transport limit still apply;
larger reads need explicit windows or artifacts.

Deployment requires the pushed daemon code and a fresh client tool catalog.
Install/start the new native shim; N06 drains and replaces an older daemon
automatically. The first pre-handshake activation is coordinated on Windows with
Dan, upgrading shim and daemon together without a cache clear. Cache clearing is
separate maintenance and still requires all users of that archive to have exited.
Because `tools/list`
changes, refresh/restart catalog-caching clients (including OpenCode when idle)
and use a new Claude process. Verify every listed tool's serialized `_meta`,
then test a real 200–350 KB `node.read` with `max_lines: 0`: inspect the actual
model-facing `tool_result` for the entire body, not just a host diagnostic
payload or an offload-file preview. The declaration alone is not live-delivery
proof.

## Failed family calls include the full operation documentation

An immediate family response with explicit top-level `success: false` now
includes **`operation_doc`**, when the requested verb resolves. This is exactly
the object returned by that family's `describe` with `of` set to the operation:
summary, full parameter schema, returns, examples, related operations and error
codes. It calls the existing `OperationRegistry.describe_full` serializer;
there is no reduced template or second documentation source.

This applies to validation errors **and other explicitly unsuccessful
outcomes**, including nested ETCH validation ledgers, exceptions converted by
the dispatcher, partial mutation acknowledgements and typed admission
refusals. The original `error`, `describe_hint`, `detail`, `changed`, warnings
and recovery instructions remain intact. **Documentation is not permission
to retry:** a failed response can still report completed or uncertain writes.
Always inspect its original ledger and recovery instructions.

The shared dispatcher delegates to its unchanged execution core and attaches
documentation only after that call returns, on a fresh top-level envelope.
No cache view, registry metadata or original result is edited. Admission,
coherence boundaries, exception mapping, advisory ordering and transport are
unchanged. If documentation generation itself fails, `operation_doc_error`
reports that secondary problem without hiding the original outcome.

Successful responses (including raw text and accepted async-job acknowledgements)
are unchanged. Unknown/missing verbs keep their existing teaching response;
there is no guessed operation document. The wrapper does not parse error text,
inspect nested job records, or retrospectively enrich background-job failures.
The policy applies to calls through the family dispatcher, not standalone
older-profile tool implementations. Exposed MCP input schemas, operation
schemas and the explicit `describe` output are unchanged.

## Export one note as a Markdown file

```json
{"verb":"read","params":{"node_handle":"[ap:K7M2QF]","output_file":"E:\\notes\\specification.md"}}
```

Call this through the `node` tool. `output_file` writes **only the node's
Markdown note string**: no name banner, generated heading, descendants,
system advisory, or Markdown rendering/reformatting. It uses the same
canonical one-layer Workflowy wire decoding as an ordinary `node.read`;
literal Markdown/HTML source, indentation, line endings, and trailing
whitespace are otherwise preserved. No metadata stripping or extra newline.

The file is strict UTF-8 without a BOM. Its parent directory must already
exist. An existing destination is replaced atomically only after encoding
and writing succeed; an encoding/write failure leaves it untouched. No
lossy replacement of invalid Unicode is performed.

The tool returns a small acknowledgement (`output_file`, `node_id`,
`bytes_written`, `encoding`, `success`), not the document. Any active system
advisory remains on that acknowledgement, **never in the exported file**.
`output_file` requires `as_raw_node=true` (the default); combining it with
structured-node mode is rejected. Omit `output_file` (or pass `null`) for
the unchanged ordinary read behavior. UUIDs, unique prefixes, and AP-IDs
use the existing shared handle resolver. No cache/journal writes or
render-pipeline changes are introduced by this artifact-delivery option.

## Replace all matching text in an existing note

`node.edit` supports a **Boolean `replace_all`** at the call level and on each
`note_edits` entry. The default is **false** (first occurrence, unchanged).

```json
{"verb":"edit","params":{"node_handle":"[ap:K7M2QF]","note_search":"old","note_replace":"new","replace_all":true}}
{"verb":"edit","params":{"node_handle":"[ap:K7M2QF]","replace_all":true,"note_edits":[{"search":"old","replace":"new"},{"search":"first","replace":"one","replace_all":false}]}}
```

Each entry inherits the top-level default unless it explicitly supplies true
or false. Thus a single entry can opt into replace-all without a global flag,
or opt out of a global true. Explicit null/numbers/strings are rejected;
unknown entry keys still fail loudly. Every entry validates before the live
note read, so a malformed later entry cannot cause earlier edits to be sent.

For each entry, the existing matching tier is chosen **once** over its input
text: if any exact, case-sensitive literal matches exist, only those are used;
otherwise the 1:1 Unicode-space fallback applies. Folded matches preserve each
original span's unchanged context (including NBSP indentation). No regex,
case-folding, whitespace-run collapsing or fuzzy replacement is introduced.

Replace-all selects **non-overlapping matches from the pre-entry text**.
Inserted replacement text is never searched again during that entry, including
when the replacement contains the search string. Later entries still see the
results of earlier entries. Empty searches remain errors; omitted/null
replacement still means deletion.

The JSON-metadata-plus-selected-diff report is retained. Entries using
replace-all add `replace_all: true` and `replacement_count` (zero on a miss).
`index` remains the first match's index in that entry's input text;
`edits_applied` and `edits_noop` still count entries, not occurrences. Default
first-match ledgers are unchanged. A miss remains a note no-op, and an
explicit accompanying name/layout change still behaves as before.

This remains **one live basis read and at most one update invocation through
the existing edit core**, not one write per occurrence. Each checked send adds
one final direct live read; the normal success path retains its after-state read.
No cache, claim, governor, broker,
encoding or protected-name path is bypassed. Top-level `replace_all=true`
requires search/replace mode; it is not valid with whole-note `note` or
`note_file` replacement. Read live before composing edits as usual.

## Replace an existing node or Anchor Point note from a file

Call the `node` family using the existing node's UUID, prefix or AP-ID:

```json
{"verb":"edit","params":{"node_handle":"[ap:K7M2QF]","note_file":"C:/notes/replacement.md"}}
```

This **replaces the entire note**, not the node or its subtree. The filename
is ignored for naming: the UUID, AP-ID, title, tags, children and position are
preserved unless you also explicitly request an ordinary `name` or
`layout_mode` edit. Nothing is captured as a new node or parsed into headings.

`note_file` must be a non-blank **absolute path on the MCP server host** to an
existing UTF-8 text file. Capture and replacement share one strict reader:
CRLF/CR become LF, while whitespace, Markdown, literal entities and any UTF-8
BOM are retained. **An empty file clears the note.** Missing, unreadable,
directory, relative-path and invalid-UTF-8 inputs fail before the edit; there
is no replacement-character fallback or partial file import.

A non-null `note_file` cannot combine with non-null `note`, `note_search`,
`note_replace` or `note_edits`, or enabled `replace_all`. Omitted/null
`note_file` leaves the existing inline edit behavior unchanged. The same
four diff modes below apply to inline and file input; the default shows only
the seams, not the imported document's bulk.

Read the existing note before replacing it. This has exactly the same
whole-note overwrite semantics as `note=`: it does not merge concurrent edits
or automatically preserve a mapped note's metadata header. If that header
must remain, include it in the replacement content.

The file is loaded at the family edge after normal admission, then the
existing `_node_edit_impl` / `_node_edit_core` path owns the update. No new
mutation transport, claim path, cache write, AP-ID allocation or UI change.

## Append or prepend note text from a file

`node.append_note` and `node.prepend_note` accept the same file source as
`node.edit(note_file=...)`, under the parameter name **`text_file`**:

```json
{"verb":"append_note","params":{"node_handle":"[ap:K7M2QF]","text_file":"C:/notes/part4.md"}}
```

Pass **exactly one** of inline `text` / non-null `text_file`: combining them,
or passing neither, is refused before any file is opened and before the note
is read. `text_file` is a non-blank **absolute path on the MCP server host**,
read with the same strict UTF-8 / CRLF-to-LF reader as `note_file`; missing,
relative, directory and invalid-UTF-8 inputs fail with no note read or write,
and an empty file is the same loud empty-text error as empty inline text.
The acknowledgement carries the top-level `path_record`
(requested/resolved/translated/host); concatenation, separator and returned
sizes behave exactly as with inline text.

## Four diff modes for every note edit

`node.edit` uses the same top-level **`diff`** mode for single search/replace,
`note_edits`, whole-note `note`, and `note_file`. This **applies the edit**,
then reports it; it is not a dry run.

```json
{"verb":"edit","params":{"node_handle":"[ap:K7M2QF]","note_search":"old","note_replace":"new"}}
{"verb":"edit","params":{"node_handle":"[ap:K7M2QF]","note_file":"C:/notes/replacement.md","diff":"seams_full"}}
{"verb":"edit","params":{"node_handle":"[ap:K7M2QF]","note":"# Updated\n\nNew body.","diff":"none"}}
```

- **`seams` (default):** for each changed old/new block, show the line before,
  its first two and last two lines, and the line after. Elide the middle with
  a line count. Long seam lines collapse to roughly 128 characters around
  each change, with ellipses at cuts; distant changes retain separate windows.
  Context faces the changed block: the preceding line's end and following
  line's start. Hunk headers carry full `+added / -removed` counts.
- **`seams_full`:** the same seams and counted middle elisions, but seam
  lines are shown whole.
- **`full`:** the historical unified diff, including the one-line
  `note was empty; N lines written` summary for an empty old note.
- **`none`:** only the JSON metadata header, for every note-write kind.

`return_diff: true` / `false` remains an alias for `full` / `none`, including
search/replace. Omit both parameters for `seams`; matching enum/alias pairs
are accepted, conflicting pairs fail. `diff` is a string enum, not the old
tolerated Boolean spelling of `return_diff`. A nonempty diff mode requires
a note operation; name/layout-only edits retain their small acknowledgement.

Every note edit returns one raw text report: pretty-printed JSON metadata
(`success`, `no_op`, `note_changed`, `node_id`, `note_chars`), followed by a
blank line and the selected diff when present. The diff is not JSON-escaped
or placed in a `diff` field. Search/replace keeps `edits_applied`,
`edits_noop`, and compact `edit_results` (`ordinal`, `matched`, `index`, plus
existing matching diagnostics); whole-note writes fabricate no search ledger.
File-backed reports also keep `path_record` in that header. `none` still
uses the one live snapshot for truthful `note_changed`; it skips diff rendering.

An unchanged note returns just metadata. **Identical matched/replacement
edits still invoke the update**: `no_op: false`, `note_changed: false`.
Empty diff does not mean zero writes. Both `note: ""` and an empty file remain
valid note-clearing operations; a search miss alone remains a zero-write no-op.

The before text comes from the edit core's **one memoized live-node snapshot**,
not the cache, decoded through the canonical note decoder. A combined name
change shares that same snapshot. The after text is the submitted semantic
replacement, not an extra readback. Inline text/newlines are unchanged;
`note_file` keeps its existing strict UTF-8/universal-newline behavior. No
normalization is introduced to beautify a diff. At most one update is invoked.
Name/note writes compare a final live read against that original raw basis and
compare the returned state with the encoded value sent, ignoring Workflowy's
presentation changes. A detected difference returns `concurrent_edit_detected`
with live/expected **wire text**, written fields and `retry_after: 0.0`; re-read,
recompute and retry. Preflight means this attempt did not send; postflight means
it sent and the observed state differs. GUI edits in the final-read-to-send gap
remain **last-writer-wins**: neither transport offers compare-and-swap. Layout-only
writes and zero-write no-ops acquire no extra N02 read. Existing admission,
pacing, cache publication and transport retry behavior remain.

**Size guard:** all nonempty diff modes retain the shared 400-line head/tail
guard. One additional elision row names the omitted diff-line region, hidden
hunks, and true whole-diff addition/removal totals (including lines already
hidden by seam projection). `seams_full` and `full` intentionally retain
whole lines; use `seams` for bounded paragraph windows or `none` for no diff.

## Node-as-a-file: line windows and note grep

Use the `node` family to inspect long Anchor Point notes without exporting
or ingesting the whole document:

```json
{"verb":"search","params":{"node_handle":"[ap:K7M2QF]","query":"architecture","context_lines":2}}
{"verb":"read","params":{"node_handle":"[ap:K7M2QF]","offset":119,"limit":61,"with_line_numbers":true}}
```

`read.offset` is a **0-based skip count** (default zero); `limit` is a positive
line count (omitted/null means through EOF). A window (`offset > 0` or a
non-null `limit`) omits the name banner. `with_line_numbers` adds right-aligned,
**1-based** note-body numbers (`120 | text`), sized to the largest displayed
number. Numbering alone keeps the name banner, unnumbered and outside the note.
Empty or past-EOF windows return empty text. Explicit `offset`, non-null
`limit`, or enabled numbering requires `as_raw_node=true` and cannot be
combined with `output_file`. Ordinary reads/exports keep their exact existing
text contract, including original line endings and trailing whitespace.

`search` accepts **exactly one** of `node_handle` (one live note) or
`root_handle` (a pinned-cache subtree, including the root). Only note bodies
are searched. Optional subtree-only `name_filter` is a case-insensitive title
substring or whole-title glob with `*`/`?`; brackets are literal, so `[ap:*`
works naturally. Filtering titles does not prune children of nonmatching
parents. Completed/suppressed nodes and child Docks remain searchable.

```json
{"verb":"search","params":{"root_handle":"[ap:K7M2QF]","name_filter":"*Spec*","query":"^## ","is_regex":true,"max_matches":50}}
```

Matching is a literal substring by default, or Python regex with
`is_regex=true`; `case_sensitive=false` by default. Each logical line is
searched independently: no cross-line matches and **no Boolean/smart grammar**.
A line with multiple hits counts once. `context_lines` defaults to 2 (0–10);
`max_matches` defaults to 50 (1–200) and caps matched lines **per page**, globally
across selected documents, not per node. `search.offset` is a **0-based count
of matching lines to skip** (default 0, no upper bound), applied after
`name_filter`. Unlike `read.offset`, it does **not** count all note lines.
Results follow the existing scope traversal order, then ascending line order
inside each note. They group by full UUID/title, mark selected page hits with
`>`, and merge overlapping context windows. Context may overlap adjacent
pages; skipped or lookahead hits shown as context are not marked/counted.
An empty query or invalid regex is an error; whitespace is valid literal text.

Every report ends with pagination metadata, including empty/past-end pages:

```text
Pagination: offset=0; returned_matches=200; has_more=true; next_offset=200.
```

Repeat the same search with the returned `next_offset` to continue past 200:

```json
{"verb":"search","params":{"node_handle":"[ap:K7M2QF]","query":"architecture","max_matches":200,"offset":200}}
```

`next_offset` is `offset + returned_matches` when another hit exists, otherwise
`null` with `has_more=false`. The existing truncation notice now teaches that
continuation. One extra matching line determines `has_more`; there is no full
scan merely to count a total. Deep offsets rescan the preceding matches.
Paging is **stateless**, not a frozen multi-call snapshot: each call performs
the ordinary live read or operation-cache capture. Changes to note text,
titles or tree order between pages can cause repeats or omissions. Keep the
scope/query/filter stable and restart from offset 0 if the source changes;
no cursor, snapshot lease or cross-request cache alias is retained.

The ONE pure line authority (`note_lines.split_note_lines`) normalizes
CRLF/CR to LF, then uses Python `splitlines()`. Empty text has zero lines; a
final terminator does not create a phantom line. Indentation, NBSP, emoji,
entities, and markup within each semantic line are preserved. Number prefixes
are display-only: do not paste them into `node.edit` replacement text.

### Coherence and scope boundaries

- Single-node windowing/grep uses the existing live read and canonical decoder
  exactly once; it does **not** take note contents from the cache.
- Subtree grep uses `current_operation_cache().view`, after the standard
  `cache_operation_boundary` capture. Canonical cache notes are already
  semantic and are **not decoded again**. There is no per-descendant API read,
  implicit refresh, cache mutation, journal publication, or new claim path.
  Existing ingestion-profile differences (including the documented nested-
  entity edge) are preserved, not repaired inside search. Use a live read
  before edits/extraction rather than treating cached line numbers as fresh.
- The standard finalizer preserves coherence advisories. Advisory lines are
  framing, not note-body lines, and never shift displayed line numbers.
- `node.search` is line-level diagnostic grep, not a tree render.
  `lightning_rod.render_search` finds matching **nodes**;
  `slice_group.sift` searches within a projected **lens**.
- `anchor.point.extract` (Node-as-a-File Part 3) moves ranges into new points
  through the existing capture/edit paths; see below.

## Extract note ranges into new Anchor Points

Call the `anchor` family, **after a live `node.read`/`node.search`**:

```json
{"verb":"point.extract","params":{"node_handle":"[ap:SOURCE]","parent_handle":"[ap:DEST]","start_line":120,"end_line":180,"name":"Standalone section"}}
```

This is **MOVE-only**: one new point receives lines 120–180 (61 lines), then
those lines are removed from the source. Bounds are **1-based inclusive**,
matching displayed numbers without translating to `read.offset`. AP-ID policy
defaults to `auto`; explicit IDs or `null` (skip allocation), `tags`, and
`target_tab` are delegated unchanged to `point.capture`. Inherits capture's
existing placement and tag policy: currently leaf captures get plain tags;
`--expand` is added only to captured roots that have children.

Batch form creates **one point per range** under the same parent:

```json
{"verb":"point.extract","params":{"node_handle":"[ap:SOURCE]","parent_handle":"[ap:DEST]","ranges":[{"start_line":120,"end_line":180,"name":"Section A"},{"start_line":900,"end_line":940,"name":"Section B"}],"tags":["topic-x"]}}
```

Every range uses the **same single live source snapshot**. All range shapes,
bounds, overlaps, and empty extracted text are checked before any capture.
Ranges cannot overlap (adjacent is legal); creates run in ascending document
order regardless of input order. Batch items inherit the top-level AP-ID
policy unless overridden. No per-range parents/tags, concatenation into one
point, append-to-existing, or mixed single/batch form.

### Ordering, evidence, and recovery

1. Resolve/read the source through the existing single-node read authority.
2. Plan all ranges via the **same `note_lines.split_note_lines`** used by reads
   and grep; no alternative line domain.
3. Delegate every create to `_anchor_point_capture_impl`. Require its explicit
   `success=true` plus one nonblank created root ID for **every** range.
4. Only then call `_node_edit_core` **once**, injecting the original source
   snapshot. Remove all ranges in one sorted splice; no blank-line cleanup.
   Replacement text is LF-joined without adding a terminal newline; reported
   `lines_after` is the actual logical line count after that join. The shared
   updater makes one final preflight read and retains its normal after-state read.

Success returns an `extracted` row (single) or array (batch), source counts,
and capture results/warnings. On a capture failure, **no source update runs**;
`error.extra` retains all attempted/not-attempted rows, known created IDs,
and the original capture result/error. A failed capture can itself have an
uncertain transport outcome: inspect the parent before retrying, even if
no ID was returned. Already-created content is never automatically rolled back.

On an ordinary source-update failure, **the source outcome is unknown**, not necessarily
unchanged: the API may have accepted the update before its readback failed.
The ledger retains all created points and explicitly requires `node.read`
before any retry or cleanup. Error codes distinguish
`partial_failure_source_unchanged` (source note not written by extraction)
from `partial_failure_source_unknown` (update attempted). A mid-operation
coherence refusal retains its universal `coherence_barrier_active` envelope
and adds the extraction ledger under `error.extra.extraction`. A detected
`concurrent_edit_detected` retains that ledger too: preflight means this attempt's
source rewrite was not sent (not that the GUI left it unchanged); postflight means
the rewrite was sent and the observed source differs. Captures remain acknowledged.
Inspect them, re-read the source and recompute ranges before retrying; never replay
the old line numbers. An inherited retry may already have sent an earlier attempt.

**Not a transaction or concurrent-edit CAS.** Only the source-rewrite stage is
checked; captures are not rolled back, and the final-read-to-send gap remains
last-writer-wins. Existing capture/CRUD may perform tag writes, readbacks or
their inherited retries; one original basis and one source-update invocation
describe this orchestrator, not total HTTP traffic. No new
retry, rollback, claim, cache writer, manifest publisher, or transport path
is introduced. All mutations inherit existing family admission, AP identity
proofs, paced CRUD, and transition copy-on-write cache publication. Completion
and suppression do not block explicit extraction. Leading mapped-note
metadata is a deliberate sharp edge, as with `node.edit`; persist mapped
Markdown changes with F12+3.

## SCRY includes the requested root's note

`node(verb="scry", params={"root_handle": "[ap:K7M2QF]"})` returns the
requested root's full note at `root.note`, plus its descendants. Size limits
reserve the root even when it occurs after descendants in the flat cache;
a retained child can no longer silently become the reported root.

With `output_file` pointing to a `.json` file, the existing SCRY exporter
preserves its children-only `nodes` layout and adds `export_root_note`
alongside `export_root_id` / `export_root_name`. The companion Markdown
includes the root note before the children, using the same fenced-note
format as child notes. Original/backup artifacts retain it too. Root
content comes from the same subtree snapshot, with no additional live GET.
Existing Dock guards, coherence advisories, and child numbering are unchanged.

SCRY remains a structured subtree export. For **only one note's Markdown
string**, without system framing or export metadata, use `node.read` with
`output_file` as described above.

## Child-aware headings and completion acknowledgements

`headings_only=true` adds a source-child marker to each real node heading,
in both Markdown and full/UUID mode:

```text
# [uuid] Parent ▸12
## [uuid] Known leaf ▸0
## [uuid] Uncached node ▸?
```

**N means ALL direct children in the operation's cache snapshot**, including
completed, suppressed and out-of-slice children—not just those visible in
the render. It is not a recursive descendant count. `▸?` means unknown, never
silently zero. Synthetic UUID-less headings have no source-child count.

This applies through the existing shared render paths: GLIMPSE DOM/cache/
frozen-modal views, Dock renders, Lightning Rod node selections, and slice/
lens renders and sift. Plain tag-vocabulary `gather` is not a node-heading
render. The same counted composer output reaches agent, modal and file.
Counts remain source-graph facts when focus, sort or filtering prunes/repeats
a node. Ordinary full-body renders and row identity are unchanged.

`node.complete` and `node.uncomplete` now return compact scalar metadata:
`id`, `name`, `completed`, `completedAt`, `modifiedAt`, `child_count`, and
`child_count_source="operation_cache"` (under the normal acknowledgement's
`detail`). Notes, nested children, raw data and legacy body aliases are
omitted. Counts are captured before mutation; null means unknown. Existing
mutation/encoding/governor/COW paths are untouched and no extra live GET is
added. Dock peek uses the same count authority and retains its existing format.

Implementation: one pure `build_direct_child_counts` index from the caller's
full pinned map, carried as result-side `RenderPolicy.direct_child_counts`.
It is computed once per render context and shared across pre-rendered rows;
never stored on cache nodes and never inferred from the projected forest.
No new request flag, wire field, traversal policy or mutation safety rule.

**Counts are snapshot metadata, not live verification or permission to
complete/delete a subtree.** Inspect relevant descendants before structural
changes, especially when the cache may be stale or the marker is unknown.

## Compact job status

`system(verb="job.status")` now returns **active jobs only**, plus
`active_count`, `inactive_count`, and inactive counts by terminal status.
It never dumps historical job bodies by default. Unknown/blank statuses are
conservatively active, using the existing lifecycle classifier.

Simple list filters:

```json
{"verb":"job.status","params":{"status":"failed","limit":10}}
{"verb":"job.status","params":{"kind":"CARTO_REFRESH"}}
{"verb":"job.status","params":{"status":"inactive","limit":20}}
```

`status` accepts `active` (default), `inactive`, `all`, or an exact status;
`kind` is an exact case-insensitive match. Lists are newest-created/started
first, limited to **50 rows by default, maximum 200**, and **32 KiB of UTF-8
JSON**. `matched`, `returned`, `omitted`, and `truncated` expose incomplete
results; total/active/inactive counts always describe the whole inventory,
not just the filtered rows. Rows contain only compact scalar identifiers and
status/timestamps—no nested progress, results, warning bodies or log paths.

Pass `job_id` for one job's **summary**, including paths, progress, counts,
top-level warnings and typed failure/recovery metadata, but no per-file rows.
Cartographer summaries also carry `outcome`, `has_errors`, `has_warnings` and
`has_findings`. Use `view: "detail"` for the complete record, in pages joined
using `detail_page`'s `next_offset` and `next_slice_offset` (send them as
`offset` and `slice_offset`). `files: "all"` or `files: "problems"` selects
file-row pages via `files_offset` and `files_limit`; `file: "<path>"` selects
one path's rows and receipts. Every one-job reply stays within **512 KiB**:
`truncated` and `message` explain continuation, and `previewed` names summary
values available whole in the detail view. Unknown view values are served with
`parameter_warnings`, not refused. List filters cannot be combined with
`job_id`. The legacy `mcp_job_status` front shares this behavior. Neither view
archives/deletes history, refreshes caches, edits claims or changes the
lifecycle. **Terminal status does not prove actual task
completion or region release**; the daemon's live hold/task state remains authoritative.

## Export archives: read what you archived

NEXUS can now read back what `node.export` archived. The pitch: keep a
small, dedicated Workflowy account that holds only the living, mutating
material — export a finished subtree to a folder (`node.export` with
`output_file`, then check `verified: true`), delete it in Workflowy, and
repeat. Everything you archive stays searchable, sift-able, sliceable and
renderable, as if nothing had been deleted.

**Set the folder once.** The archive folder is the GLIMPSE extension's
**Archive folder** option (one absolute folder; empty = archive reads off).
The daemon remembers the last value the extension reported, so archive
reads never need a connected tab.

**Two ways archives answer, both read-only.**

1. **Automatically.** Any read whose handle — an AP-ID, a UUID or its
   8-hex segment, or every handle in a list — finds nothing in live
   Workflowy is answered from the active archives. The result opens with a
   banner nobody can miss:
   `⚠️⚠️⚠️ ARCHIVED NODE: NOT IN WORKFLOWY. Served read-only from export file ‹path› (exported ‹date›). Tell the human.`
2. **On request.** `include_archives: true` on a search, sift, slice group
   or render adds each active archive's subtree under the parent it was
   exported from, every archived node marked 📦, after the live children.
   With the flag off and archived material under your scope, the result
   ends with a one-line footer saying how many subtrees were left out.

**The rules that make it safe.** Live always wins: archives are consulted
only when live Workflowy has no match, and they never make a live handle
ambiguous. The **parent rule** — in Dan's words — *"the exported subtree's
parent must stay in Workflowy. It may be moved, but it must stay under the
root you search or sift from, and suppression, completion and every other
rule apply exactly as if nothing had been exported."* An archive whose
parent exists nowhere is **orphaned**: it sits at the top level, reachable
by its own handles, and `system.archives.mount` re-roots it under a live
parent.

**Writes never touch archives.** A write aimed at an archived node refuses
with the file and the export date (batches skip the item and carry on).
To copy archived material into Workflowy, read it and capture it — or use
`anchor.point.promote` with `mode='derivative'`, the one sanctioned copy:
it builds new live nodes from the archive's verbatim records with fresh
AP-IDs and leaves the file untouched.

**Never served from archives:** beacon (symbols are live only — disk is
truth), `node.export`, `output_file`, `push_to_modal`, the GUI.

**The inventory.** `system(verb='archives.list')` lists every file in the
folder with exactly one state — `active` (its root is gone from
Workflowy), `dormant` (the root still lives), `superseded` (a newer export
shadows it), `skipped` (a whole-vault export), `orphaned`, `unreadable` —
with the reason, and works with no tab connected. Its describe text is the
one canonical rulebook:
`system(verb='describe', params={'of': 'archives.list', 'detail': 'full'})`.

**First try:** set **Archive folder** in the GLIMPSE options (then reload
the extension), export a small subtree into it with
`node.export output_file=…` and check `verified: true`, delete the subtree
in Workflowy, and read one of its old handles — you should see the banner
and the 📦 mark. Then run `system(verb='archives.list')`: the file shows
as `active`.

## Project Maps: a pointer in extension settings

NEXUS does not own your Project Maps — it only remembers where they live.
The GLIMPSE extension's **Project Maps location** option (Options → Note
sessions and Anchor Docks, directly under the Default Note Sessions parent)
takes one Workflowy node: its UUID, the 8-hex start of it, or an `[ap:…]`
AP-ID. It is a pointer and nothing more. What goes into a Project Map, and
how many you keep there, is entirely up to you.

`system(verb='config.get')` returns the stored pointer as
`project_maps_location` — the trimmed string, or `null` when unset — so an
agent can look there at the start of a task and scripts can use the same
value to find a project's map. NEXUS never resolves, validates or requires
anything inside that node; leave the option blank to keep no location.

## Release notes

**Export archives are readable (Archive Reads).** `node.export` archives
can now be read: a handle that misses live Workflowy is answered read-only
from the configured archive folder under a ⚠️ banner;
`include_archives=true` adds archived subtrees to searches and renders,
each node marked 📦; writes refuse archived nodes and
`anchor.point.promote mode='derivative'` copies them back with fresh
AP-IDs; `system.archives.list` / `system.archives.mount` inventory and
re-root them. Set the **Archive folder** option in the GLIMPSE extension
to switch it on.

**Salvageable subtrees retired (S06, Mutation Coherence Wave A).** The
emoji-prefixed "Notes salvage" feature (NotesSalvageContext, park/restore
moves, `🧩 Notes (salvaged)` relocation, and the emoji-based DELETE guard)
was retired before Mutation Coherence Wave B. Cartographer-mapped source
regions are disk-authoritative; unmatched Workflowy-only nodes beneath them
may be removed by F12+1. Keep durable commentary/captures outside mapped
source regions (or in supported disk-backed workflows). The developer vault
was verified to contain no salvageable subtrees, so no migration was
required. The detached WEAVE `enchanted`/`direct` worker modes and the JEWEL
UUID write-back machinery were removed in that earlier pass. Cartographer
refresh, Markdown generation (with inline phase 8), and bulk apply now run as
daemon tasks; CUT-2B removes the unused weave_worker entry and the remaining claim/PID/heartbeat/file-lock coordination.
The one in-process registry carries job/Q3 footprints and memory cooldowns; writes keep their shared 1.0 s schedule and persisted breaker/Retry-After protection. Old coordination artifacts are ignored and need no cleanup. Pending cancel replies retain their legacy outcome and add cancellation_pending: true.
