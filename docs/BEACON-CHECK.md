# beacon.check — the read-only beacon integrity check

## What it is for

`beacon.check` lists, by file and line, every beacon problem that
Cartographer or the tag writers would trip over, and writes nothing. A beacon
curation run calls it at its start, to see what to fix, and at its end, to
confirm the repository is clean. It replaces "run `beacon.map.refresh` and see
what breaks" as the way to find beacon problems.

The check defines no rule of its own. Every row reports a decision an
existing function already makes: the shared block parsers, Cartographer's own
single-file mapping, the tag writer's extent and anchor helpers, and the
comment validator. Each row names that function in its `authority` field.

## How to call it

Exactly one target per call:

- the source-map `Root:` node of a repository (every mapped file in it):
  `beacon(verb='check', params={'node_handle': '<Root: node>'})`;
- a mapped FOLDER or FILE node:
  `beacon(verb='check', params={'node_handle': '<FOLDER or FILE node>'})`;
- one file on disk, mapped or not:
  `beacon(verb='check', params={'file_path': '/mnt/e/…/some_file.py'})`.

`node_handle` takes the family's node-handle forms (exact UUID, its 8-hex
first segment, a unique longer prefix, or `[ap:CORE]`). `file_path` must be
absolute; it is resolved on the daemon's host like every other tool path
(`/mnt/<drive>/…` ⇄ `<DRIVE>:\…`), and the result echoes the resolution as
`target.path_record`.

Optional filters and paging parameters:

- `min_severity` (`error` | `warning` | `info`, default `warning`): rows below
  it are counted, not listed. `min_severity: "info"` lists the info rows,
  empty comments (`empty_comment`) among them.
- `max_rows` (integer ≥ 1, default 300): page size, with no upper bound.
- `offset` (integer ≥ 0, default 0): eligible rows to skip, with no upper bound.
- `codes` (array of code names): list only these codes; misspellings are parameter
  errors, not an empty result. Omitted selects all codes; `[]` selects none.
- `finding_ids` (array of strings): list only these findings. Omitted selects
  all identities; `[]` selects none. Requested identities absent from **all**
  current findings are returned in `missing_finding_ids`, never assumed fixed.

Findings are globally ordered by severity (`error`, `warning`, `info`), file,
line, code and finding_id, then filtered by all supplied filters, then paged.
`eligible_total` counts the entire filtered listing; the page is
`eligible[offset:offset + max_rows]`. `rows_truncated` counts eligible rows
**after** this page; `has_more` is true exactly when some remain. `next_offset`
is `offset + len(rows)` then, otherwise null (also for an empty/past-end page).
Counts and the summary always cover **all** findings, not just eligible rows.

Repeat with `offset: next_offset` and the same target/filters. Compare
`scope_fingerprint` on every page: unchanged pages concatenate to the full
eligible listing; if the fingerprint changes, restart. It is a SHA-256 of
canonical JSON of sorted per-file observations (`canonical_file` and
`file_sha256`, or explicit missing/unreadable identity), including clean and
marker-free files. Each file is observed once, but folder files can be observed
at different moments; this is not a multi-file snapshot or a stored cursor/lease.

The files are read in a worker thread, so the daemon keeps serving other
clients during a whole-repository check.

Verb errors use the family's existing codes: `invalid_parameter_combination`
(both targets or neither), `node_handle_unresolved` (no unique node; an
ambiguous prefix carries the candidates), `map_target_invalid` (the node is
not a FILE, FOLDER or `Root:` node), `beacon_target_unsupported` (a single
file whose extension no beacon parser or writer reads), and
`invalid_parameter_value` (a relative, missing or directory `file_path`, a
path the daemon's host cannot open, `max_rows` below 1 or `offset` below 0).
Invalid types and enum values use the shared parameter errors. Once selected,
a file that disappears before its read is `file_missing`; undecodable or
otherwise unreadable source is `file_unreadable` with an explicit identity.
An unexpected per-file analyzer exception becomes one `check_failed` row at
line 0 with `detail.exception` as `Type: message`; other files continue unchanged.
Only a whole-engine infrastructure failure is `operation_failed`.

## The result

```
{"success": true,
 "operation": "beacon.check",
 "summary_line": "...",
 "target": {"kind": "file" | "folder", "resolved_uuid": ..., "header": [kind, value],
            "root_abs": ..., "path_record": ...},
 "counts": {"files_checked", "files_skipped", "files_missing", "files_unreadable",
             "files_with_problems", "by_severity": {"error", "warning", "info"}, "by_code",
             "by_file": {file: {"total": N, "by_severity": {"error", "warning", "info"}}}},
 "rows": [...], "rows_truncated": 0, "min_severity": "warning",
 "eligible_total": N, "has_more": false, "next_offset": null,
 "scope_fingerprint": "...", "missing_finding_ids": []}
```

A report with problems is still `success: true`: the check succeeded. A
`node_handle` call fills `resolved_uuid`, `header` (the node's `Path:` or `Root:`
line) and `root_abs` (the source-map root); a `file_path` call fills
`path_record` and leaves the others null. `counts` and `summary_line` always
cover every finding, regardless of filtering or paging. `counts.by_file` gives
each file's total and severity counts, including zeros for clean files.
`files_missing` includes both missing map entries and files that vanish before
read; `files_checked` retains the selected readable-at-collection inventory size.

**The summary line** follows the batch writers' style
(`BEACON-BATCH-PARTIAL-SUCCESS.md`, "The one-line summary"):
`N files · E errors · W warnings · I info`, then in parentheses either the one
error or warning row as `file:line code — message` (the message cut to 110
characters), or, with several, the distinct error codes then warning codes
with counts, at most four, then `, ...`. With no error or warning, the base
alone. Example:
`412 files · 3 errors · 0 warnings · 189 info (writer_anchor_mismatch ×2, duplicate_id ×1)`.

**Each row** carries `file` (relative to the source-map root on a node call;
the resolved path on a `file_path` call), `line` (1-based; 0 means the whole
file), `code`, `severity`, `view`, `authority`, `beacon_id`, a one-sentence
`message` naming the block and the consequence, and `detail`.

Each row also carries `finding_id`, `kind`, `producer`, `observed`, `members`
and `fix`. `finding_id` is `bf1:` plus a full SHA-256 over the canonical real
file, code/kind, owned metadata bytes and semantic anchors. Display roots,
producer and absolute lines do not enter it. Two worktrees are different files.
`observed` pins the current file hash, metadata ranges and the U03/U04 physical
member witnesses (section, opener byte, raw block hash, owned-range hashes and
anchor). Identical members are explicitly ambiguous; an ordinal is not proof
of identity after source changes. A later fix must revalidate the witness.

`fix` describes available resolutions, never executes them: class
`auto | choice | manual | none`, resolutions with `params_schema`, and an
optional recommendation. Auto on a read is not consent. Choices include `keep`
(no write or stored waiver) and a bounded correcting action where geometry is
known. Otherwise manual instructions name the missing evidence. No resolution
offers deletion of code/prose. Field/comment work remains independent of label
work. Duplicate-id keeper/renames and id resemblance are never auto-selected.
Same-id compatible merges and safe aliases use the existing pure planners'
verdicts; skipped groups retain their exact reason and physical members.

`insert_wrapper` offers only boundaries after the existing bracket, never a
field-end position that would close the wrapper before `]`. `delete_metadata`
ranges remain member-bound scanner ownership, not the bounded `read_range`:
when no whole-block write extent is owned, only the shown field ranges are
available. A later wrapper close or another block's terminator never supplies
that missing ownership.

One-step whole-block `delete_metadata` is offered only where the scanner owns
that complete write extent. With missing wrappers, conflicting ids, inline
ambiguity, ambiguous pairs or uncertain lexical ownership, repair first using
the offered syntax/pair/id resolution. Then remove the well-formed beacon
through ordinary `tags.remove` (the final label removes the block), or the
equivalent existing removal. Check again for any further repair: never reuse a
stale finding, borrow a later terminator or override the scanner's ownership.

Pair choices are explicit operands, not an inferred association: `pair_close`
uses member indices `opener` and `close`, or `opener` plus `open_only: true`.
Persisting an ambiguous pair/open-only choice needs `new_id`; `""` expressly
requests collision-free allocation. `rename_id` and each `assign_ids.new_ids`
entry also accept `open_only: true` or `close` as the current close member's
`opener_start_byte` (duplicate rows expose `detail.close_candidates`). A unique
assigned close follows its opener's rename. No choice silently renames an id,
selects a keeper or pairs a near spelling; allocated ids and possible identity
and cached-anchor effects are advisory consequences. These check catalogue
operands are accepted by `beacon.fix` with the row's `finding_id` and `observed`
witness; its describe text documents application, optional preview and freshness.

## Applying a selected finding: beacon.fix

Pass the finding's identity and **unchanged `observed` object**, plus one of
its offered resolution names and the operands shown in its `params_schema`:

```json
{"verb":"fix","params":{"items":[{
  "file_path":"E:\\repo\\module.py",
  "finding_id":"bf1:<the check's full hash>",
  "observed":"<copy the check's observed OBJECT here, not this string>",
  "resolution":"set_field",
  "params":{"member":0,"field":"id","value":"chosen-id"}
}],"dry_run":false,"atomic":false}}
```

The example's `observed` placeholder must be replaced by the actual object;
the tool does not accept a string or an arbitrary byte patch. Use exactly one
`file_path` or `node_handle` on each item. The handle must name a mapped **FILE**,
not a symbol or folder. Absolute paths use the same host translation as check;
path-only fix needs no Workflowy tab. Read `beacon.describe` for the precise
schema. No preliminary preview, approval, refresh or stored finding is required.

`dry_run: true` reads, plans and verifies without a lease or a file write.
Each `preview` row gives `before_sha256`, `candidate_sha256`/`after_sha256`,
`unified_diff`, affected ids/symbols and advisory `consequences` (including
possible UUID/history or stale-note-anchor effects). Previews in a file are
independent against one observation; they are not promises about a later apply.
An explicit rename is not refused to protect Workflowy history. A relocation
or paired rename is one verified metadata plan/commit, not write-then-restore.

Apply re-derives findings from current disk and validates the supplied witness:

- A missing finding is `stale`, never proof that the requested repair ran.
- With an unchanged file hash, current physical member locators are accepted.
  After outside edits, only a uniquely unchanged member and semantic anchor
  can rebind; shifted lines or an old ordinal alone never select a copy.
- Identical members need the original observed file version or a newly listed
  choice. Later items of this same request follow its own proved edit-offset
  maps, so earlier repairs do not falsely stale a later selected identical copy.
- Unsupported resolutions/invalid operands are local refusals with findings.
  `keep`, `retry` and `preserve_opaque` are read-only noops, not stored waivers.
  Syntax/decode failures that require manual repair get no fake executable fix.

`add_close` accepts only a physical line start within the span section after
the opener, or EOF; it inserts one close line without adding a blank line and
preserves whether the last line is terminated.

Default execution groups canonical files in first-appearance order, preserving
input order within each file and global input order in the returned ledger.
Each file takes one existing Q3 lease through its commits; one busy or
unresolvable file does not block other files. A path without a FILE node uses
the nearest mapped FOLDER/Root whose path covers it. With no covering mapping,
the row reports `lease: none_unmapped` and commits under the shared final-hash
check alone. No retries, TTL, new governor or refresh are introduced.

Overlapping requested edit domains fail the involved rows. `atomic: true`
explicitly asks for all-row preflight and one all-target admission before any
write; a knowable preflight/admission failure leaves zero writes. It is **not**
multi-file rollback: external JIT changes or a later commit failure can still
follow earlier successful writes. Non-atomic safe neighbours continue.

The standard mutation acknowledgement keeps the full result in `detail`:
`rows` (one per input), `files_changed`, `success`, `changed`, `partial_success`,
`summary`, `summary_line`, `findings`, `dry_run`, `atomic` and `batch_aborted`.
Rows retain identity, target, resolution, lease and actual outcomes. Statuses
include `preview`, `committed`, `noop`, `stale`, `refused` and `error`;
`error_kind` distinguishes invalid items, unavailable paths, analysis/ownership
or parameter failures, collisions, admission failures, stale source, proof/IO
and cleanup failures. Inspect both `changed` and the rows on failure: there is
no implication that `success: false` means nothing was written. The ack's
aggregate findings describe each written file's final version, observed once;
files without commits and noop/error/stale/preview rows retain their planning
evidence. Committed rows keep final findings for their affected ids and the
acted-on finding under `finding`. Check again only after other changes, and
refresh separately when wanted.

## The codes

The one code table is in repository-root `beacon_findings.py`, re-exported by
`beacon_integrity`; describe projects it rather than maintaining a second list.
The original 22 names, severities, views and authority strings are retained.

| code | severity | view | authority | what the row means | how to fix it |
|---|---|---|---|---|---|
| `file_unreadable` | error | file | `_parse_disk_beacons_for_language / map_codebase (strict UTF-8 read)` | the file cannot be read or decoded; it carries an explicit unreadable identity when no whole-file snapshot exists | restore readable UTF-8 source and retry; `detail.exception` names the failure |
| `file_missing` | warning | file | `resolve_cartographer_path_from_node` | the map lists a missing file, or a selected file disappeared before its read | restore the file; for a stale map entry, refresh its folder so the map stops listing it |
| `mapper_parse_error` | error | mapper | `_is_cartographer_parse_error_sentinel over map_codebase` | the outline builder failed, so no beacon in the file or affected Vue section reaches the map; message and finding_id are stable across repeated checks of the same bytes, with the timestamped sentinel only in `detail.name` | fix the syntax error the row's `detail` names, then check again |
| `unterminated_block` | error | parser | `parse_*_beacon_blocks` | no structural closing bracket: this record is bounded at the next marker, non-comment line, wrapper/section end or EOF; later independent blocks remain visible | insert at a recorded candidate; auto only under the scanner's exact unambiguous rule, otherwise choose the intended boundary |
| `unknown_kind` | error | parser | `parse_*_beacon_blocks` | `kind=` is neither `ast` nor `span`: the block is skipped and does not exist for Cartographer or the writers | set `kind=ast` or `kind=span` |
| `missing_id` | error | parser | `parse_*_beacon_blocks` | the block has no id: Markdown drops it; other languages map it under a fallback name the writer cannot address | add an `id=` line, unique in the file |
| `unknown_field_key` | warning | parser | `parse_beacon_field_lines / beacon_field_unknown_keys` | an unknown assignment remains opaque, with its raw occurrence retained | choose a known key/value, preserve opaque text, or delete the selected field only |
| `legacy_field` | info | parser | `parse_beacon_field_lines` | `role=` (deprecated) or `comments=` (read as `comment=`) | write `comment=` instead of `comments=`; drop `role=` (or move its text into the comment) |
| `duplicate_id` | error | mapper | `_duplicate_beacon_ids` | two or more blocks share this id: the refresh adds a 🚨 warning node; independently anchored copies remain writable, while exact-id-only ambiguity returns candidates | inspect the physical members before choosing which ids to change |
| `block_not_mapped` | error | mapper | `map_codebase + parse_cartographer_note_for_preview` | this physical copy reaches no mapped node, even when another copy of its id maps | relocate, convert to span, delete metadata only, or keep; no automatic FILE-node capability |
| `two_blocks_one_symbol` | error | mapper | `map_codebase + parse_cartographer_note_for_preview` | one target carries several physical records; an incompatible merge needs choices | same-id compatible merge is proposed auto; otherwise choose survivor/fields, union labels, convert, delete metadata only, or keep |
| `kind_changed_by_mapper` | warning | mapper | `map_codebase` | an explicit kind maps as the other kind, such as Markdown AST demotion without a later heading | choose span conversion or a current target; missing-wrapper ownership is separate |
| `no_slice_labels` | warning | mapper | `map_codebase (_canonicalize_slice_labels)` | the beacon maps with no label, so it is in no slice | set explicit `slice_labels` |
| `slice_labels_derived` | info | mapper | `map_codebase vs parse_*_beacon_blocks` | absent labels were derived from role or id prefix | write the label explicitly |
| `span_without_close` | info | parser | `parse_*_beacon_blocks` | a legal open-only span runs to file/section end; an empty paired span is closed | explicitly add/select an exact-id close, or keep |
| `orphan_close` | info | parser | `parse_*_beacon_blocks` | a structural close has no unique eligible exact-id opener in its section; every close is examined, not marker mentions | select a physical pair or explicit close id, delete selected orphan metadata, or keep |
| `block_end_mismatch` | error | writer | `_beacon_block_extent_for_language vs parse_*_beacon_blocks` | no complete whole-line extent; bounded field/subrange ownership remains distinct | inspect the shown metadata boundaries; field-local edits may still proceed without rewriting ordinary source |
| `writer_anchor_mismatch` | error | writer | `beacon_attachment_index via _find_anchor_matched_beacons vs map_codebase` | a mapped block has no complete whole-line tuple at this target; field/subrange work can remain valid | inspect extent and current target; tuple absence is not permission to create another block |
| `writer_anchor_ambiguous` | error | writer | `beacon_attachment_index via _find_anchor_matched_beacons` | several complete AST blocks physically attach to this target | inspect all physical members and the shared planner verdict; independently identifiable members remain writable |
| `writer_target_ambiguous` | error | writer | `_resolve_anchor_target_line` | the symbol's qualified name names several outline nodes (a property getter and setter, two same-named functions); a name alone cannot select a target | select an independent physical witness where available, or choose a uniquely named target; inspect candidates rather than assuming a blanket refusal |
| `comment_rejected` | info | comment | `validate_beacon_comment_text` | the comment would be refused if sent as a new comment; on disk it maps and is kept as is; the validator's message says why | reword it before re-sending it through `tags.ensure`; otherwise optional |
| `empty_comment` | info | comment | `beacon grammar (comment recommended)` | the block has no comment; label work remains valid | explicitly replace/append a description, or keep |
| `check_failed` | error | file | `check_beacon_file` | an analyzer failed for this file | inspect the exception, recover the reader/source and retry; no structural repair without evidence |
| `noncanonical_terminator` | info | parser | `scan_beacon_structure bracket_range` | complete comma/space terminator is accepted | normalize metadata only, or keep |
| `missing_wrapper` | error | parser | `scan_beacon_structure wrapper ownership` | no owned HTML/C close at the metadata boundary; later arrows/headings are not owned | insert wrapper at the shown boundary, edit bounded metadata, delete selected metadata, or keep; inspect retained text/headings |
| `mixed_comment_ownership` | warning | parser | `scan_beacon_structure write_ranges` | metadata shares an ordinary comment | edit only metadata ranges, retaining the reported ordinary text, or keep |
| `duplicate_field` | warning | parser | `parse_beacon_field_lines occurrences` | all repeated occurrences/raw values are retained | identical repeats may normalize; otherwise choose a value/occurrence, delete a selected field or keep |
| `inline_field_ambiguity` | warning | parser | `parse_beacon_field_lines diagnostics` | one-line metadata has several possible splits | choose a parsed alternative, explicit field value, or keep the opaque text |
| `pair_ambiguous` | warning | parser | `scan_beacon_structure exact pairing` | several exact-id span members could pair | choose physical opener/close, retag, add a close, delete metadata only, or keep; never auto-pair by spelling |
| `lexical_uncertainty` | warning | parser | `scan_beacon_structure lexical evidence` | ownership is uncertain for this local record | choose a bounded metadata field/extent; otherwise manual source edit; definite examples are not live targets |
| `writer_anchor_foreign` | error | writer | `beacon_attachment_index vs mapper projection` | metadata attaches to a target whose mapper projection does not carry it | inspect physical candidates/capability, then relocate/convert/edit metadata or keep |
| `implicit_kind_promoted` | info | mapper | `beacon_attachment_index effective_kind` | an implicit kind is promoted to AST | retain it, make span explicit or relocate metadata |
| `id_unlike_symbol` | info | attachment | `beacon_id_resemblance` | file-local R3/R7/R8 spelling evidence names another symbol/path | keep, rename explicitly, relocate or edit descriptive metadata; no hard error or automatic rename |
| `vue_capability` | info | writer | `beacon_attachment_index section capability` | actual template/style bounds are known; tags deltas stay script-only, legacy absolute actions remain supported | choose supported metadata/relocation action, or keep; not parser corruption |

**A close-id typo shows as two rows together:** `span_without_close` on the
opener and `orphan_close` on the close (MasterSpec R3). Both rows are info;
list them with `min_severity: "info"`. Adjacent closes are examined too.

## What a quiet result does not prove

The six parsers now project one structural scanner. Openers must begin an
actual comment body; Python strings/docstrings, JS strings/templates, SQL
quotes, shell heredocs, YAML scalars and Markdown fences are not live blocks.
Actual comments inside nested JS template expressions remain live. Lexer or
outline failure is not a prerequisite that rejects every beacon in a file:
uncertainty is local and only bytes requiring that uncertain ownership are
withheld from destructive use.

Incomplete records keep their own bounded fields and diagnostics. They never
steal the next block's id/labels. Brackets in values are not terminators;
one-line fields and comma/space terminators are accepted. Exact-id closes are
scanned once, including orphan/adjacent closes, distinct nested/interleaved
spans and empty spans. Ambiguous same-id relations do not authorize deletion.
Complete mixed C/JSDoc/HTML comments expose metadata subranges, not ownership
of surrounding ordinary text. Missing wrappers followed by prose arrows,
mermaid edges, HTML comments or a heading after a stray close are reported as
`missing_wrapper`, with retained text and intervening headings; those later
bytes are not owned metadata. Canonical, inline and blank-gap complete wrappers
remain valid. Context slurps use these same recorded ranges; malformed metadata
cannot consume following human context.

Remaining limits are separate from that structural contract:

- Python, JS/TS and Markdown AST mapping and the writer anchor compatibility
  helper now share `beacon_attachment_index`. Gaps cross real comments and
  syntax-owned decorators, never executable code, bare strings or generator
  methods. Explicit spans and paired implicit spans are not AST candidates.
  Markdown explicit AST still attaches to the strictly-next parsed heading;
  the index retains intervening prose as gap evidence. Plan and all seven
  legacy writers consume the same physical selector and field planner.
- `duplicate_id` counts all physical open records using the shared exact stripped
  nonempty-id rule, including unmapped copies. Each copy retains its own findings.
  The selector resolves each anchor before considering id multiplicity.
  Vue's legacy lost-bracket path uses section-owned records; an incomplete
  template delete does not hide a complete script neighbour.
- The planner and writers return file-local R3 enclosing-path, R7 near-spelling
  and R8 other-symbol resemblance suggestions, using only the retained snapshot.
  They are information, never selection, refusal or automatic rename authority;
  unrelated concept ids stay valid. The check exposes `id_unlike_symbol`;
  protected gesture propagation remains the integration work.
- A second Markdown block on a heading can show as `block_not_mapped`, not
  `two_blocks_one_symbol`. Markdown writer targets remain heading lines.
- The check's attachment/writer view includes every current outline target,
  untagged twins included. A structural record's field/subrange ownership is not a
  claim that every legacy writer already uses byte-local splices.
- Planned and no-plan legacy tag mutations splice snapshot-owned bytes and use
  the shared binary atomic commit, preserving the BOM, physical endings,
   literal controls and EOF outside the edit. Duplicate-id refresh reconciliation
   uses unique composite or independent sibling identities rather than the first
   copy; unresolved copies retain `_beacon_reconcile_report` candidates without
   assigning UUIDs by order. Protected refresh projection of this new report
   remains an integration concern. Kit tag/untag also consume the shared physical
   selector and ordinary field planner, preserving wrappers and opaque fields;
   kit creation retains its decorator-aware insertion and label-based id convention.
  A range crossing unowned code is refused for that node with `plan_mismatch` evidence,
  not repaired by the commit guard. Ordinary Markdown label edits never run a
  whole-document C0 cleanup; that remains an explicit claimed repair.
- Duplicate-AST hygiene, comment-alias canonicalization, the explicit C0
  repair, generation publication/EOF and explicitly requested id resolutions,
  and standalone kit tag/untag also use
  the shared byte proof and binary publisher. Duplicate hygiene now groups
  explicit AST blocks by the shared physical attachment: only same-id,
  compatible groups merge, splicing labels/comments at the nearest member
  without changing its wrapper or other fields. Broken or conflicting groups
  retain member/range evidence as local choices; independent groups proceed.
  Structured preparation-result propagation in the protected refresh remains
   supplied by U09's components but not joined to the protected gestures until
   work package J applies `BEACON-ROBUSTNESS-INTEGRATION.md` at rebase. Alias selection now consumes shared field occurrences:
  only the `s` in an unambiguous, complete `comments` key is deleted; canonical
  collisions and incomplete/uncertain records retain local candidates while
  independent aliases proceed. Strings and fenced examples are not records.
  The explicit whole-Markdown C0 pass returns `not_utf8`/`io_error` findings
  instead of aborting the independent map; claim and cancellation still raise.
  Both passes return committed events, ranges/hashes and file-local failures.
  Undecodable Markdown reaches the mapper's parse-error sentinel, but the
  untouched protected FILE-header refresh still requires a `LINE COUNT` that
   this sentinel lacks. BR-09's component returns typed unreadability and
   preserve-existing policy; J must join header/error handling with R1 retention
   before completing that refresh. A header-only workaround would enable deletion.
  Generation text/file EOF normalization shares one physical-row tail rule:
  interior Unicode separators and CRLF/CR/LF remain intact. Phase 7 prepares all
  heading metadata before one publication; phase 7.5 only analyzes duplicates
  unless given a current explicit U06 resolution. Duplicate ids never veto
  regeneration or choose a keeper. The pure preparation also retains every
  omission and prose-relative relocation with original metadata and manual
  actions; reverse Markdown still does not regenerate spans.
  Protected phase 6 still writes beacon-free text early. Its whole-file
   publication join remains J's ready R5 work order, not activated here.
  The standalone Markdown JSON CLI already uses the pure preparation and one
  publication (expected absence for a new target), and prints the diagnostic
  rows as well as actual heading counts; it has no separate raw source writer.
- Plan-based Vue template/style tag deltas remain outside its script-only
  capability (`operation_not_supported_for_target`); the no-plan helper keeps
  its existing template/style updates and deletes. Parser and mapper views
  are section-local and template records are counted once.
- The snapshot map seam performs no ignore discovery, so checks do not emit
  one ignore-file log per file. General path/directory mapping logs are unchanged.
- The 22 original code names remain; additive structural/choice codes carry
  current witnesses and offered resolutions. Checks never execute those actions.

And what it does not cover at all:

- **T3.1 · A disk folder path** is refused. A folder is checked through its
  mapped FOLDER or `Root:` node, because the `.nexusignore` rules that decide
  which files a folder holds exist only inside the map refresh.
- **T3.2 · Files the map does not list yet** (created since the last refresh)
  are not part of a folder check. Check them by `file_path`.
- **Read races:** every file's rows belong to one binary snapshot and carry its
  hash. Files in a folder may be observed at different times; no read lease is taken.
- **T3.4 · Non-UTF-8 files** are `file_unreadable` rows and are not analysed
  further.
- **T3.5 · A daemon without tree-sitter** reports JS/TS/Vue blocks as
  `block_not_mapped`.
- **T3.6 · Concurrent checks, several users, remote hosts:** not designed for.

## Read-only guarantees

The verb declares `side_effects: ["pure_read"]`. It writes no file, makes no
Workflowy call, patches no cache, takes no lease, starts no job, runs no
refresh and needs no Workflowy tab (it never reads the `#wfy-suppress` list).
Handles resolve through the family's own resolver against the pinned
operation cache, paths through the shared tool-path resolver, and the files
are read, parsed and mapped in a worker thread. The tests prove the checked
files byte-for-byte and mtime unchanged, and that the named write, lease, refresh,
job and Workflowy entry points are never entered.

The single-file mapper uses one immutable binary source snapshot for its
outline, beacon attachment and `LINE COUNT`. A leading UTF-8 BOM is absent
only from the parse view; CRLF, CR and LF are physical line boundaries, while
Unicode separators inside strings remain characters. Directory mapping
contains an unreadable or failed source entry and continues its siblings,
retaining a diagnostic node for that entry. The check now uses this same snapshot
seam for all of its views; protected refresh reconciliation is unchanged.
Protected file/folder refresh, terminal-state aggregation, F12+2 warning
propagation and F12+3 preparation/publication hooks remain work package J's
ready work order in `BEACON-ROBUSTNESS-INTEGRATION.md`, outside this program,
at rebase before merge. U09's executable preparation, retention, aggregation and
terminal components are delivered; a pure mapper component
passing tests does not mean those joined gestures have been deployed.
J's outstanding joins are R1 (retain the old subtree after outline failure),
R2 (preparation findings on new and existing files), R3 (all-file findings and
honest partial terminal state), R4 (F12+2 refresh warnings), R5 (route phase-6
generated bytes plus its pre-write snapshot and live forest into
`prepare_generated_markdown_beacons`, then `publish_generated_markdown_beacons`
once, instead of the early write and later disk phases), R6 (phase-8 outcome separate from committed
disk outcome), and R7 (satisfied legacy noop success). The protected refresh
caller currently projects `_carto_identity_seed_report`, not the new
`_beacon_reconcile_report`; that ambiguity projection is also J's R1/R2 join.
U09's final preparation supplies physical duplicate occurrences, including
unmapped copies; the existing protected warning call has not been retargeted.
Phase 7's existing warnings channel already carries findings/omissions/relocations
on this branch. It still counts every warning as a skipped beacon; J must project
diagnostic rows separately from failed heading outcomes and preserve the parent
generation claim (no nested lease/Q3 admission). Pre-phase-6 disk-only omissions
and original prose-relative positions are observable only at that R5 join.

## Relation to map.refresh and tags.ensure

- **`beacon.snippet`** reads one immutable snapshot for its physical lines,
  own metadata, current symbol/span and findings. Closed spans stop before the
  whole closing comment; `format="json"` exposes that paired U01 record's id,
  line/byte/wrapper extents, pair state and section under `location.span.close`.
  Empty spans remain closed. A legal open-only span reads to EOF (or its Vue
  section end), under the existing shared window/head-tail guard. Explicit
  legacy text anchors still select their documented target range.
  Default `format="text"` is unchanged when there are no findings; otherwise
  the readable window stays, followed by a compact report: the notice line,
  one line per warning-or-higher finding (with its `finding_id`), short lines
  for any degradation, and a pointer to `beacon.check` / `format="json"` for
  the full payload. Info-only findings add just the notice and the pointer.
  JSON returns `text`, `location`, `resolution_strategy`, `findings` and the
  same-snapshot `observed` identity, with `finding`/`analysis_error` when present.
  Snippet findings include member-linked closes and file-level failures, with
  complete file counts and a `beacon.check` locator for unrelated file rows.
  Analysis failure is data, never a read veto. Parse failure retains bounded
  metadata/span text as `degraded`, not a claimed full AST resolution.
  Duplicate ids use current independent attachment/signature evidence; a stale
  cache line alone cannot choose. Unresolved targets return candidates. Once a
  node resolved, extraction failure is `source_extraction_failed` with file/id
  and resolved identity, never another UUID-text symbol/suppression lookup.
  Reads change neither source bytes nor mtime and never repair metadata.
- **`beacon.map.refresh`** writes Workflowy and, on claimed runs, rewrites
  source. Use `beacon.check` to find beacon problems, fix them on disk, then
  run one refresh to bring the map up to date. A folder check sees what the
  last refresh mapped, so refresh after adding files.
- **`beacon.tags.ensure` / `beacon.tags.remove`**: the writer-view codes
  (`block_end_mismatch`, `writer_anchor_mismatch`, `writer_anchor_ambiguous`,
  `writer_target_ambiguous`) and `duplicate_id` identify cases to inspect, not
  a prerequisite check or blanket refusal. The shared selector can write an
  independently anchored duplicate or identifiable twin, and field-local edits
  can preserve damaged or opaque neighbours. Only unresolved physical targets
   or unowned requested bytes are withheld for that item; safe items proceed.
  Invalid identifiable text, duplicate physical targets, raised item exceptions
  and file-local admission failures now skip only their own rows by default.
  Invalid comments preserve disk text while valid labels apply, with explicit
  failed field outcomes. `atomic` is the opt-in all-row/all-target veto, not
  rollback. Failure/noop/JIT rows carry the check's same-snapshot findings and
  candidate/field evidence; `already_absent` is safe absence, while an unknown
  span id with labels on current candidates is `note_beacon_id_missing_on_disk`,
  not a claimed removal. See `BEACON-BATCH-PARTIAL-SUCCESS.md` for the ledger.
- **`beacon.sync_node`** keeps the protected writer's fail-closed noop success
  mapping, adds the reason and retains candidate/diagnostic fields. Its additive
  findings are explicitly current disk **after** the legacy writer, not that
  writer's precommit snapshot. The legacy direct tool now keeps that same
  post-writer projection. F12+2's permitted inner loop retains per-node returned
  findings/noop evidence and a distinct satisfied counter, but keeps its existing
  fail-closed errors/skips and refresh trigger. The protected F12 joins above
  remain J's; see `BEACON-ROBUSTNESS.md` for fields, cadence and activation state.

## Standalone agent-kit check

The kit vendors this same finding core, input projection, scanner, attachment
rules and pure hygiene/alias planners. For its existing `.py`, `.js`, `.jsx`,
`.ts`, `.tsx`, `.sql`, `.sh`, `.yml` and `.yaml` files, `code_map.py check`
prints `relative/path:line: severity code: message`, then its own catalog/slice
problems. Empty ids are not duplicate groups; a repeated nonempty id produces
one group row with all physical members. A legal open-only span and id
resemblance are info, not command failures. Errors, warnings, unreadable/map
failures and catalog/slice problems exit 1; info alone exits 0. The existing
parser gate and missing-catalog behavior stay unchanged.

Writer-view authority explicitly names the kit's metadata capability rather
than implying a daemon operation is available. The kit still edits only its
supported Python/JS/TS targets, offers no fix command, and does not add Markdown,
Vue or the daemon's `.bash` suffix merely for fixture parity.

`code_map.py read` consumes those same U01 records over its retained snapshot.
Its established CLI presentation includes a closed span's whole opening and
closing metadata; an empty pair is still closed, and an open-only span now reads
to EOF rather than being refused. A span does not need an otherwise successful
AST map. An AST block still prints only its attached symbol (including Python
decorators when recorded), intentionally unlike the daemon's own-block view.
The existing CLI size/head-tail guard stays in place. `check`, not `read`, is
the kit's finding listing; neither command rewrites source.
