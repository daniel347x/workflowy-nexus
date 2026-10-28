# Beacon robustness — local work, visible choices

## What changed and what is active

The source snapshot, six structural parsers and Vue section views, physical
selector, byte-local plans and verified binary publication now agree. Damaged
metadata does not consume its neighbour; an ambiguity names physical candidates
instead of choosing a keeper, id or relocation by spelling. Compatible local
hygiene is automatic only on already-authorized claimed mutation paths. Reads,
`beacon.check`, snippets, lists and dry runs never repair source.

`beacon.check` pages current facts with content-derived identities and offered
resolutions. `beacon.fix` applies an explicitly selected offered action, with
optional preview, current witness revalidation and file-local outcomes. Tags
are exact-label deltas; sync/legacy F12 remain absolute name-label sets, with
explicit note comments authoritative and absent comments preserving disk.
`beacon.snippet` retains useful text and typed findings; `format="json"` exposes
the same snapshot/geometry/observation alongside the readable window. The
maintained `BEACON-CHECK.md` and `BEACON-BATCH-PARTIAL-SUCCESS.md` describe those
verbs and their exact public fields. No new public verb is added by U09.

Allowed U09 front edits retain findings on the legacy direct tool and preserve
per-node data plus a distinct satisfied counter in F12+2's inner loop. The
mapper now maps `.markdown` as Markdown, `.bash` as shell, and YAML consistently
with its existing beacon-language table. Folder `.nexusignore` selection is
unchanged; an explicitly targeted ignored file remains targetable. Unsupported
files retain the mapper's generic-leaf behavior and an explicit analysis skip,
not a new refusal and never a warning.

**J-1 joins FILE/new-file preparation and failed-scope retention (R1/R2/BR-09).**
One final observation supplies mapping and findings; unreadable or unidentifiable
outlines retain existing symbols, and identifiable failed Vue sections stay
intact while healthy sections proceed. Optional alias/group cleanup exceptions
are warning/repair data, not a reason to stop a usable refresh. A registered FILE
refresh keeps its source JSON, journal and buffered trace only when the file
changed, failed or was interrupted; an unchanged file leaves its evidence row,
and the probe's `source_json_file` still names the artifact that was written.
U09's original component-ready delivery is history. **J-2 joins folder/terminal
aggregation and R7:** every attempted file keeps its result, useful mixed work
completes with warnings, and cancellation retains saved work. FILE, FOLDER, bulk and Markdown
jobs use one registered writer for the `summary-head/2` record and complete
`files.jsonl` rows; the accumulator updates summaries, and batch aggregation runs
only when detail is requested. Full evidence is read through
`system.job.status(job_id, view='detail')` pages. A run-list-derived terminal
error is stored as a readable bounded preview plus `error_derivation`; complete
detail rebuilds the original string from the complete error items, without a
writer-side evidence read (Q6). Bulk receipts stream to `nodes.jsonl` at each
returned batch; later file finalizations append deltas without retaining earlier
rows. Markdown appends its publication receipt and primary row before inline
phase 8, then appends the refresh rows and envelope through the same writer;
even its best-effort failure path uses that writer, with a record warning.
Stored rows keep a
planned outline as a null-or-count/digest `reconcile_children` marker, with
finding anatomy and the existing public projection complete. A caught mapper
exception keeps its exact CPU-latch provenance as local data, including ordinary
coded file errors; unattributed/typed run-wide errors remain failures. Only
explicit satisfied/safe-absence noops allow single-F12 follow-up refresh.
J-3 joins bulk R4 and keeps real in-flight commit receipts on cancellation
without changing successful-call counters or stop timing.
J-4 joins Markdown R5/R6: observe the prior target, prepare the complete document
in memory, publish once, then report the inline refresh separately. A local
refresh failure retains committed disk and completes with warnings; exactly
attributed phase-8 mapper exceptions stay local without clearing the CPU latch.
The one primary file's complete generation findings stay in flight until its
phase-8 handoff: an exception keeps them, a successful refresh's `[]` clears
them, and every exit releases the hold. A failed refresh without a changed-work
receipt reports unknown effects, not an assertion that nothing changed. Failed
heading preparations are warnings; a refused publication never claims generation
succeeded.
No deployment is claimed here.

## Using findings without blocking independent work

List with `beacon.check` (`min_severity="info"` when all choices matter), page
with `next_offset` while `scope_fingerprint` stays unchanged, then send the
selected `finding_id`, unchanged `observed` object and offered resolution to
`beacon.fix`. Re-list stale choices. No stored findings database, waiver,
mandatory preview, approval, severity gate or automatic retry is introduced.
A finding of error severity is data, not a reason to fail a refresh whose map
is independently usable. Source remains the current-finding store; historical
repair/omission/relocation events live in the operation's returned detail.

Unknown identity or unowned proposed bytes affect only that item/scope. Safe
neighbours continue. The existing governor, claims and Q3 admission still own
daemon overlap/cancellation, including waiting for an in-flight commit before
release. A final byte-hash check refuses an external save that precedes it;
there is no cross-file rollback. An uncooperative editor/git replacement
strictly between the final hash and atomic replace remains the narrow external
last-instruction race, **not** an OS compare-and-replace guarantee. No retry,
TTL, heartbeat or cooldown pretends to close that race.

## Operation cadence

| Operation | Observation/check cadence | Activation note |
|---|---|---|
| Refresh preparation | Full findings once per final file version, reusing the map/attachment snapshot; optional claimed C0→alias→groups, then one current read | J-1 preparation/retention and J-2 folder/terminal aggregation joined; FILE/FOLDER evidence lands in the one registered `summary-head/2` writer, read back whole through joined detail pages |
| Writers (`tags.*`, sync, legacy, fix) | Target-local planning/after-state proof at each commit plus one final touched-file observation (fix: written files); no per-item full analysis in tags plans or repository-wide integrity gate | Tags aggregate all resolved files' final findings with per-beacon row subsets; fix aggregates written files' final findings with per-affected-id committed-row subsets and retains the acted-on finding; legacy/sync observation remains current disk after the writer |
| F12+2 | Per-file final analysis, not per candidate; retain ordered node outcomes, actual commit receipts and follow-up refresh findings | J-3 joins protected bulk outcomes; satisfied noops may refresh but do not count as changed disk files; rejected nodes remain visible beside useful work |
| F12+3 | Observe before mutation; prepare all heading/beacon/EOF bytes in memory; one atomic publication, then full phase8 findings | J-4 joins U08 and R6: publication, refusals, omissions/relocations and refresh outcomes stay separate; no retry or intermediate beacon-free write |
| Check/list/snippet/dry run | Read-only, current snapshot; a per-file analyzer failure becomes data and safe neighbours continue | Check paging and snippet text/JSON are active components/fronts |

The named J-1 through J-4 callers implement the joined cadence; component-only
tests remain complementary evidence, not daemon gesture activation witnesses.

## Integration component envelope

`workflowy_mcp.beacon_refresh_support` has no job runner, claim acquisition or
Workflowy executor. The caller selects paths and supplies an already live claim.

| Entry point | Inputs and returned data |
|---|---|
| `prepare_beacon_source(file_path, *, dry_run=False, claim_id=None, repo_root=None, cancel_check=None, log_callback=None)` | `file`, `status`, `changed`, `map`, current `findings`, `warnings`, actual committed `events`, complete ordered `repair_outcomes`, `diagnostics`, `check_target`; readable files also `snapshot`, `file_sha256`, `analysis_inputs` and `duplicate_source_beacon_ids` when analysis inputs permit. `analysis_error` is advisory `check_failed`, never a valid-map veto. Read failures have `error`, `error_kind` (`not_utf8`/`io_error`), `unreadable_identity` and canonical missing/unreadable findings. Generic unsupported source is `skipped`/`skip_reason`, with the ordinary generic map. |
| `beacon_refresh_projection(prepared, *, existing_children=())` | `state`: `valid`, `parse_failed` or `partial_section`. Whole failure gives `preserve_existing=true`, `reconcile_children=null`, `create_allowed=false`, `delete_allowed=false`, retained UUIDs. Valid empty gives false and `[]`. Vue unique section type/start-tag identity gives `sections`, `retained_sections`, `retained_uuids` while healthy sections remain plans. Unknown failed identity retains the file, never guesses. Returns findings/warnings and U03 `reconcile_report`, `ambiguities`, `ambiguous_count` as data. Caller consumes it BEFORE sentinel flattening/seed/reconcile. |
| `aggregate_beacon_outcomes(outcomes, *, warnings=(), events=())` | Complete `files` for every attempted file, including new-file failure; `findings` is at most 200 rows, but `findings_total`, `findings_by_code`, `findings_truncated` cover all rows. Each file keeps full findings, errors, actual disk status, historic node/repair/omission/relocation detail and a `check_target` for current paging. `events` is uncapped; warnings are uncapped with exact `warnings_total`, `warnings_by_type`, `warnings_truncated=0`. `files_changed`, `changed`, `summary_line` are actual outcomes, not an assertion of rollback. In-process snapshot/analysis objects are omitted from serializable aggregation. |
| `beacon_job_outcome(aggregate, *, infrastructure_error=None, cancelled=False, phase8=None)` | `completed`, `completed_with_warnings`, or `failed`; exact `successful`, `failed`, `skipped`, `partial_success`, `success`, optional infrastructure error and cancellation data. Mixed useful/failed files are completed with warnings; no useful work or run-wide infrastructure/cancel is failed. Severity alone never makes the run fail. Separate `phase8` keeps a failed refresh alongside committed disk. Its complete `files` final finding sets (including clean `[]`) replace earlier current sets without losing historic events; capped top-level previews are NOT an input. Counts describe the primary file outcomes; phase8 detail separately describes follow-up refresh outcomes. |
| `beacon_legacy_outcome(writer_result)` | Delegates U06 result evidence, retains `writer_success`, `noop_reason`, `satisfied` and classified `success`. Safe-absence wire synonyms normalize to `already_absent`; only that and `already_satisfied` without an error are satisfied noops. Unknown/rejected noops remain failures. The allowed bulk seam copies only additive classification and restores original success; J R7 owns the protected gate/dispatcher success join. |

Preparation does not pre-read then mix versions: existing hygiene passes own
their observations/verified commits; one final component snapshot feeds its map
and full analysis. BR-02's refused `source_changed_since_plan` ends optional
hygiene, takes one fresh snapshot of the external bytes for both map/findings,
and never reruns hygiene. All actual repair/skipped events remain beside those
current facts. No full checker is placed inside a candidate loop.

BR-09's ordinary-FILE path now returns typed failed-file data before the
successful-header gate: bytes/mtime, the old FILE note and descendant UUIDs stay
unchanged, with unavailable header freshness reported as data. No LINE COUNT is
invented and no failed scope is seeded/reconciled. Usable FILE headers retain
the existing freshness checks.

## Evidence and curation handoff

`tests/test_beacon_refresh_support.py` tests component cadence/retention/counts;
`tests/test_beacon_integration_contract.py` tests the real J-1 joins,
front acknowledgement projection and four-corrupt-shape × four-operation
locality matrix. Real J-2 witnesses cover folder counts/status, exact mapper
attribution, saved cancellation and single-F12 noop classification.
Real J-3/J-4 witnesses cover bulk node history, one generation publication,
committed-disk/failed-refresh outcomes and saved work on cancellation.
Component-only controls do not establish gesture activation. Repository-local disposition/touch/expected
change registers keep attribution, exact deltas and named external destinations.
The Conductor owns full-tier committed-tree evidence and verify; worker targeted
results are not acceptance or deployment claims.

CF-17/20 are starting data, not fresh live-check counts: the old report's four
real candidates were the detached server `_shape_glimpse_family_return` block
(A9-D14), module-level `f8_slice_policy.py` blocks formerly at 1/764, and
`transforms.py` formerly at 1 (A9-D15). Re-find their current positions before
choosing relocate/convert/delete/keep. Review the earlier `frozenDomCapturedTree`
relocation evidence too; the JS last-line marker currently hides no later beacon.
Do not treat old fixture/docstring rows as live beacons. None of these real
placements is curated by this implementation.

What the curation run depends on: the beacon-module fixes (U01–U08) and the thin front edits, whose public signatures remain compatible. What only J adds: J-1 retains failed outlines and returns preparation findings (R1/R2); J-2 carries complete folder/terminal outcomes (R3) and accepts only explicitly satisfied noops (R7); J-3 joins bulk outcomes (R4); J-4 publishes F12+3 once (R5) and retains committed disk beside phase-8 refresh outcomes (R6). Source joins, committed-tree certification and deployment remain separate claims; Dan decides when curation runs.
