# Beacon batch partial success

## Decision and scope

`beacon.tags.ensure` and `beacon.tags.remove` continue past item-level text,
resolution, planning, collision and file-local admission failures. Their legacy Lightning
Rod modes inherit the same behavior through `_lightning_rod_mutate_beacons`.

U06 deliberately supersedes the older Q3/SPEC-v5 default batch-wide vetoes for
invalid identifiable items, raised item exceptions, duplicate-resolved nodes,
physical disk-target collisions and a busy/unresolvable file. Each input has
its own ordered row; independent files can proceed unattended.

The governor, coherence boundary and lease substrate are unchanged. Admission
uses the same `BeaconAdmission` as `beacon.fix`: one lease per canonical file
by default, held through that file's JIT commits and released only after its
worker completes, including cancellation. Only explicit `atomic: true` uses
all-target acquisition. No retry, queue, cooldown or automatic refresh is added.

Per-item cost grows with file size (measured 5.89 s/item on the 45,200-line `server.py` copy in a 10-item call), so unattended callers should keep each call's committed work well inside the connector's 900 s (100 items would be roughly 589 s at that measured rate).

## Item-local outcomes

- An unresolved, ambiguous, or out-of-scope symbol keeps its error/candidate row
  and is never written. Other eligible items proceed.
- A returned preflight `outcome="failure"` (for example an invalid anchor,
  beacon identity conflict, or unsupported target) keeps its error row and is
  never sent to JIT planning or the writer. A conflicting beacon comment is
  NOT one of these failures (see Comment conflicts below).
- Preflight noops still skip writing. JIT failures/noops and commit failures
  retain their established per-item behavior.
- Files execute in first-appearance order; items within a file keep input order
  and are JIT-replanned against the current
  file, including prior same-batch writes. Final-hash checks and per-write
  atomic replacement remain. Returned rows always keep global input order.
- Invalid labels fail that item. An invalid ensure comment preserves the disk
  comment while valid labels still apply. Its row retains the actual committed
  or noop status plus a failed `field_outcomes` entry (`preserved_disk`); overall
  `success` is false and `partial_success` reflects safe work that did land.
  Atomic treats that field failure as a preflight veto, before any write.

The planner parses and fingerprints one immutable binary observation. The
executor parses its precheck observation, verifies owned byte splices in memory,
then delegates publication to `beacon_edits.commit_beacon_edit_plan`: binary
same-directory temp, flush/fsync, mode copy, final existence/hash check, replace.
Unchanged bytes (including BOM, mixed endings, controls and EOF) stay exact.
Proof failures carry expected/actual evidence as `plan_mismatch`; IO failures
carry `disk_write_failed`. Neither rolls back an earlier item. An uncooperative
save strictly between final hash and replace remains the bounded T2 race, not
an OS atomic compare-and-replace guarantee.

Tag deltas select current AST/heading attachments before considering duplicate
ids. Independently anchored copies remain writable; exact-id-only ambiguity
returns candidates and the check's same-snapshot `finding_id` on failure,
noop and JIT rows. Executable operands pin the physical section, opener byte,
block hash and source anchor, so two copies are not a disk-target collision.
Incomplete claimed blocks are retained as extent evidence, never treated as
permission to create beside them. Detached orphans are reported and preserved
without blocking a genuine create.

Ordinary plan-path and all seven legacy language updates splice
only requested field values. Field order,
prefixes/wrappers, blank lines, implicit kind, role, snippets, show_span and
opaque unknown assignments remain byte-identical. A duplicated field returns
its occurrences as field-local candidates; independent fields can still land.
Known fields in incomplete blocks can be spliced without consuming their tail.
Whole-block deletion still requires owned destructive geometry.

## Comment conflicts (2R4E1P)

A different existing beacon comment is a WARNING, never an error, and never a
preflight failure. Under the default `comment_mode: fill` the requested
comment is not applied, the existing comment is kept, and the labels are still
ensured: the row carries `comment_outcome: "kept_existing_conflict"` plus a
`warning` naming both texts (existing vs requested), whatever its status
(committed or noop). The batch result reports kept conflicts in its top-level
`warnings` list and `detail.summary.comment_conflicts_kept`. The public
acknowledgement lifts the same list to its `warnings`. Re-running with
`comment_mode: replace` (explicit overwrite) or `comment_mode: append`
(extend) applies the requested text through the same pipeline; a comment-only
edit is `tags.ensure` with the label already present plus `replace`/`append`.
`tags.remove` accepts no comment or comment_mode.

For an existing block, label-only `preserve` reads disk only (including an
absent comment); `fill` on empty disk uses the requested text, never a stale
note fallback. Creates may seed a scoped `BEACON (` header comment. Header
reads stop at a blank line or `---`; span text/context/docstrings do not supply
fields. Note-derived comments are validated after transport decoding; a field
error preserves disk text while valid labels land. Accepted trailing commas,
brackets and whole quote pairs are quote-protected on serialization and compared
as parsed semantic text. Historical unquoted loss cannot be reconstructed.

The shared planner owns legacy `absolute_labels`: name-order canonical
dedupe, reported disk-only removals, additive headerless adoption, and explicit
note-comment authority with old/new conflicts (absence preserves disk). The
seven no-plan helpers consume the same physical selector,
planner and byte publisher. Vue uses section-owned records: legacy template/style
absolute updates and deletes remain supported; tags deltas stay script-only.
Markdown uses MD_PATH plus the caller's occurrence among all current headings;
creates retain a supplied note id, including a duplicate, minting only when none
is carried. Minted ids check every current record id (all sections, complete or
not); explicit plan sets also include already allocated creates. Repeated random
collisions fall back to deterministic numeric allocation, never a refused create.
Ordinary
synchronization still prefers an independently attached note id over
an unbacked cache ordinal. The opt-in
`intent="rehydrate"` restores the chosen heading's scoped id/kind/show_span/comment
and name labels even when another heading carries the same id. The roundtrip's
phase-7 adapter now uses that intent in pure preparation, with occurrence counted
over all live headings before filtering tagged candidates. It publishes once,
returns every ordered heading outcome and copies duplicate findings and
omission/relocation rows into its legacy warnings channel. Shared ids remain
choices (`assign_ids` / `keep`), never automatic keeper/`--dupN` decisions.
Original disk-only omissions/positions require the pre-phase-6 snapshot at J's
R5 join; the current adapter can still report source-forest span omissions.
Satisfied
legacy noops return a reason without writing; the protected F12 dispatcher still
maps every noop to unsuccessful until its separate integration change.
That success mapping is J's ready R7 join, not an activated gesture change.
The other protected joins R1–R6 remain outstanding too; their exact work order
is `BEACON-ROBUSTNESS-INTEGRATION.md`, applied outside this program at rebase
before merge. Duplicate-id reconciliation returns
`_beacon_reconcile_report` in the pure mapper; the protected refresh caller
does not yet project it. Candidate, diagnostic and field data can also be lost
in protected F12 projections until J; owned batch and sync fronts retain
them now. Batch findings describe the snapshot each row planned against;
sync findings explicitly describe current disk after the protected writer,
not an invented precommit witness. Neither observation changes legacy success.

U09's allowed F12+2 inner-loop edit adds `summary.node_outcomes` (the complete
returned per-node finding/candidate/noop data) and `summary.nodes_satisfied`.
It does **not** change `errors`, `nodes_skipped`, `node_updates` or the returned
successful count that currently triggers refresh. Unknown noops/rejected writes
are never counted as satisfied. R4/R7 must consume the added data before a
satisfied noop can refresh and the protected job can report honest mixed status.
The legacy direct tool now uses the same explicitly post-writer current-disk
projection as sync. Full final F12+2 findings belong once per file through J,
not one analyzer run per candidate. See `BEACON-ROBUSTNESS.md` for cadence and
the complete aggregation envelope; this batch's existing success semantics
are not silently replaced by the new job-presentation adapter.

## Global infrastructure versus local outcomes

Only an invalid whole envelope/root scope or required shared cache, resolver
or suppression-list failure refuses the whole batch. Once that infrastructure
is available, unexpected per-item preflight/JIT exceptions are local errors.
Duplicate-resolved nodes and physical-target collisions fail every involved
row, not innocent neighbours. Independently anchored equal ids do not collide.

Default admission is **per canonical file**, with same-file deduplication.
`overlap_conflict`, `footprint_unresolvable` or `admission_failed` leaves that
file unchanged; other files continue. Existing governor acquisition/release
and non-abandoned commit workers are reused, not reimplemented. This remains an in-request disk batch, not a new detached
job; it performs no Workflowy mutation, cache patch, or automatic map refresh.

## The atomic opt-in ([ap:LPC157] residue)

`atomic: true` (batch-level, `beacon.tags.ensure` and `beacon.tags.remove`;
default `false`) requests an all-row preflight and admission veto: when ANY
item fails preflight (including an invalid comment field, raised exception or
collision), every other eligible row becomes
`not_run_due_to_batch_preflight_failure` with zero writes and no lease. The
veto runs before lease acquisition; failing items keep their evidence. If
preflight passes, one all-target acquisition admits all eligible files; any
admission failure leaves zero writes. A mid-batch commit failure is still reported per item — each
  write uses a final hash check and atomic replacement, and no batch rollback exists. The default
(false) keeps the per-item partial-success behavior this document describes.

## The one-line summary ([ap:LPC157] residue)

The batch result carries a top-level `summary_line` (surfaced as
`detail.summary_line` on the public acknowledgement), for example:
`5 committed · 0 noop · 1 failed (sym: beacon_identity_conflict — <reason>)`.
With exactly one failed item the parenthesized detail names its symbol,
`error_kind`, and a bounded reason; with several it lists the distinct
failure kinds. `detail.summary` keeps the counts.

`beacon_identity_conflict` means the cached identity does not identify the
current physical target. Inspect the returned candidate/anchor evidence; a
refresh is not a required step and does not itself resolve a swapped identity.

## Reading the existing result

The public acknowledgement keeps the full ledger under `detail`:

- `success=true` only when every row is committed or noop with no failed field.
- `changed=true` when at least one operation committed, even if `success=false`.
- `detail.partial_success=true` for mixed successful/failed item outcomes when
  a batch-global refusal did not abort preflight.
- Noops have reasons: safe absence is `already_absent`; a stale span id with
  requested labels on current candidates is `note_beacon_id_missing_on_disk`,
  with `rename_candidates` and a warning, never proof that those labels were
  removed. The protected sync dispatcher remains fail-closed on noops.
- A successful noop plus a failed item can be partial success with no writes.
- `detail.summary` reports committed/noop/preflight-failure/commit-failure
  counts; `detail.results` has one ordered row per normalized operation.
- `detail.summary_line` condenses the ledger into one actable line.
- `detail.files_changed` lists only committed files, deduplicated. Full findings
  (the same shape as `beacon.fix`/`check`) are observed once per resolved file at
  call end, including files with only noop/error rows. The aggregate contains
  one row per `finding_id`; each ledger row has its beacon's final subset and
  singular `finding` (duplicate-id first). Candidates, diagnostics, field
  outcomes and planning hashes remain target-local; `findings_observation`
  identifies the final version, and analysis exceptions are advisory data.

Inspect the ledger rather than interpreting `success=false` as zero writes.
Address the reported errors and retry failed/not-run items, not already
committed/noop items. A global safety refusal still requires resolving that
condition first; do not repeatedly re-fire against a live lease conflict.

## Review and regression surface

`server._lightning_rod_mutate_beacons` delegates admission and ledger logic
to `beacon_batch`, which reuses `beacon_fix.BeaconAdmission`. The normalizer
retains local text outcomes. Physical selection and delta planning remain in
the shared pure mapper; `_compute_beacon_write_plan` hands its existing
snapshot to the target-local evidence projector; the batch alone observes final
file findings after all rows execute, never in each plan or preflight.
Registry text advertises the partial-success policy. No other
batch operation adopts it implicitly:
`tag.batch`, merge/purge, node edits, F12 pipelines, and governance retain their
own contracts.

`tests/test_lightning_rod_mutation_modes.py` covers the 50-item/one-failure case,
ensure/remove/final-label deletion, failure positions, complete ordered ledgers,
same-file JIT sequencing, all-failure/noop cases, public acknowledgement,
unexpected item exceptions, local busy files, explicit atomic zero-write veto,
and cancellation cleanup. `test_beacon_batch_local_admission.py` covers text,
physical collisions, exact finding identity, public evidence and cancellation.
Existing duplicate/collision, JIT/CAS, dispatcher,
and governance regressions remain part of validation. Lease-observer test
doubles test caller sequencing only; they do not replace the governor's own
unchanged safety tests.
