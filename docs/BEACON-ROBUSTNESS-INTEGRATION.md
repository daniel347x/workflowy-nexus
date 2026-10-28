# Work package J — protected beacon gesture joins

## Delivery boundary and cold start

**J-1 joins R1/R2 and BR-09 on main's one-daemon paths.** FILE preparation uses
one final map/check observation; failed scopes retain the FILE note and UUIDs
before header completion or seeding. Identified failed Vue sections are protected
while healthy sections reconcile. Discovered files use the existing live claim,
and unreadable new files return failed-file data instead of sentinel FILE trees.
Optional alias/group hygiene exceptions remain nonfatal diagnostic/repair data
(contract amendment NE35OC); C0, cancellation and CAS semantics are unchanged.
A registered FILE refresh keeps its source JSON, journal and buffered diagnostic
trace only for a file that changed, failed or was interrupted; an unchanged file
(changed false, no error, no node operation) drops those artifacts while its
probe's `source_json_file` path still names the artifact that was written (R-a).

**J-2 joins complete folder outcomes, terminal propagation and R7.** Each attempted
file is represented, including new-file failures; useful mixed work completes
with warnings and exact counts. Caught mapper exceptions keep their CPU latch
and explicit file/phase/type/message provenance without failing useful neighbours.
Unattributed and typed run-wide failures remain fatal. Saved complete rows survive
cancellation; a generic exception code alone does not make a file error run-wide.
FILE, FOLDER, bulk and Markdown jobs write their evidence through one registered append-only
writer over a `summary-head/2` record: the writer appends complete rows to
`files.jsonl`, the accumulator updates summaries, and batch aggregation runs only
when detail is requested. Full evidence is joined from
`system.job.status(job_id, view='detail')` pages. A failed run's headline error
stays readable and preview-bounded in the record/proof beside an O(1)
`error_derivation` marker; complete detail rebuilds the exact original text
from the complete error items (Q6). Legacy and markerless history is not rewritten.
Bulk receipts append to `nodes.jsonl` per returned batch; repeated file
finalizations append deltas. Markdown appends its publication receipt and primary
row before phase 8, then the phase-8 rows and envelope. Every Markdown status
path, including a failed-import or write fallback, uses the same writer and
reports a record warning rather than falling back to a plain file write.
Single-F12 accepts only real successful writes or explicitly
satisfied noops, retaining refusal evidence otherwise. **J-3 joins bulk R4:**
ordered node evidence, actual disk-change accounting, final-file findings and
refresh warnings survive; in-flight commit receipts also survive cancellation
without changing the existing checkpoint or successful-call counters.
**J-4 joins R5/R6:** the old target is observed before mutation; all generated
heading beacons and EOF bytes prepare in memory and publish once through U08.
Publication and inline refresh retain separate outcomes, including committed
disk beside a local failed refresh. Exact phase-8 mapper provenance is consumed
by the same terminal adapter without clearing its CPU latch or changing primary
publication counts. Only the one primary file's complete generation findings stay
in flight through its phase-8 handoff: exceptions keep that set, clean success
replaces it with `[]`, and every exit releases it. An exception without a complete
effect receipt reports unknown changed state. Failed heading preparations surface
as warnings, and a refused publication never claims generation succeeded.
No setup or warning gate is added; deployment is separate.

U09's **component-ready** delivery and the historical branch/main tables below
are preserved as history, not current activation evidence. Real J-1 witnesses
in `tests/test_beacon_integration_contract.py` enter protected daemon FILE and
folder paths with no mocks of the hook under test. Explicitly component-only
controls do not establish a joined gesture. Targeted evidence is the worker's;
the full tier and verify belong to the Conductor at the committed tree.

Before edits: re-measure `git rev-parse main`, the named qualified symbols and
current body hashes in the measurement table; compare branch/main AND `a345dc7`/main.
**Stale line numbers are not edit instructions.** Retain main's
task/CPU/artifact/governor shape, not the branch's older orchestrator bodies.
Use existing operation-cache boundaries, claims and Q3 leases. No new runner,
lease nesting, Workflowy executor, retry, age/TTL or findings store belongs in
these joins.

Aliases: acn=`src/workflowy_mcp/client/api_client_nexus.py` (methods are on
`WorkFlowyClientNexus` unless underscore-prefixed); srv=`src/workflowy_mcp/server.py`;
mgp=`src/workflowy_mcp/markdown_generate_pipeline.py`;
cjg=`src/workflowy_mcp/carto_job_governance.py`; ww=`src/workflowy_mcp/weave_worker.py`.
The components are in `workflowy_mcp.beacon_refresh_support` (support below),
U08's preparation/publication in repository-root `markdown_roundtrip`.

## R1–R7 exact obligations

The seed obligations and branch-behavior descriptions below are historical;
the current gate and measurement table is the live progress record.

> R1 acn _plan_carto_file_refresh/refresh_file_node_beacons → beacon_refresh_projection before sentinel promotion/reconcile; consume preserve_existing/failed-section identities, do not seed [] as a parse-error result.

| Protected symbol / input | Component call and fields to copy | Branch behavior still outstanding / joined witness |
|---|---|---|
| acn `_plan_carto_file_refresh` (main split), `refresh_file_node_beacons`; complete R2 prepared map plus the actual existing file children, under the existing parent claim | `policy = support.beacon_refresh_projection(prepared, existing_children=existing_children)` **before** FILE-header completion, sentinel promotion, seed or reconcile. `state`, `preserve_existing`, `reconcile_children`, `create_allowed`, `delete_allowed`, `sections`, `retained_sections`, `retained_uuids`, `findings`, `warnings`, `reconcile_report`, `ambiguities`, `ambiguous_count`. Whole failure: `reconcile_children is None` means no descendant replacement/create/delete, NEVER `[]`. Valid empty: `[]` is authoritative. Partial Vue: reconcile healthy sections only; retain the explicitly identified failed section's existing subtree, without reconciling that scope. Unidentifiable section failure retains the file. After U03 seeding, project the newly attached `_beacon_reconcile_report` too. | Current FILE sentinel can replace symbols; folder promotion can seed `[]`. `test_r1_r2_retention_before_seed_and_uuid_recovery`, `test_vue_section_parser_exception_keeps_healthy_section_and_existing_uuid`, `test_parse_break_restore_retains_every_uuid_and_valid_empty_is_distinct`. |

BR-09 must join **R1 and R2 together**: non-UTF-8 Markdown C03 has no decoded
snapshot and no honest LINE COUNT. Do not add a count or relax the protected
header gate on this branch: that would turn today's loud failure into symbol
deletion. On J's retained-error route, report unavailable header freshness as
data, keep the old FILE note/descendants, and reconcile no failed scope. Preserve
recognition of both `⚠️ Parse Error` and `⚠️ MD Parse Error` by the existing
producer-exact predicate; spelling is not proof of successful parsing. U04's
ordinary-FILE success witness is stored in the repository at
`tests/beacon_robustness_witnesses/c03_required_refresh_witness.patch`, SHA-256
`a5e9a77e4b79a84f6aadca4fc7987be3669f0e1e53c7c989f50b1d960aa339b2`;
J imports/retargets it with retention, not a caller monkeypatch. Repository-local
component witness: `test_br09_non_utf8_markdown_preserves_bytes_mtime_and_existing_nodes`.

> R2 acn _prepare_carto_source_file → prepare_beacon_source, returning structured rows beside messages and map; both existing/new file preparation use it.

| Protected symbol / input | Component call and fields to copy | Branch behavior still outstanding / joined witness |
|---|---|---|
| acn `_prepare_carto_source_file` on main; the preparation region in `refresh_file_node_beacons` on branch; folder newly discovered file path too. Caller-selected absolute path, repo root, existing live `carto_claim_id`, `dry_run`, cancellation callback | `prepared = support.prepare_beacon_source(path, dry_run=dry_run, claim_id=carto_claim_id, repo_root=repo_root_abs, cancel_check=cancel_callback, log_callback=log)`. Feed its **same** `snapshot`, `map`, `analysis_inputs` to downstream consumers without rereading. Copy `file`, `status`, `changed`, `file_sha256`, `findings`, `warnings`, `events`, `repair_outcomes`, `diagnostics`, `analysis_error`, `error`, `error_kind`, `unreadable_identity`, `check_target`, `duplicate_source_beacon_ids` when present. Dry/unclaimed never call writers. Claimed C0→alias→groups→one final map/check; BR-02 CAS refusal ends optional hygiene, reads external bytes once for map/findings, no retry. | Existing caller reads only `changed`; new files bypass preparation. `test_preparation_gates_order_one_final_map_and_analysis`, `test_many_candidates_do_not_multiply_full_analysis`, `test_br02_external_save_after_hygiene_plan_is_one_fresh_map_and_check`, `test_r2_r3_folder_clean_corrupt_unreadable_and_new_files`. |

**Transferred MUST MODIFY seam (UnitSpec §3/§11.2):** acn
`_detect_duplicate_source_beacon_ids(nodes)` receives only the constructed mapped
`source_root` at its sole production caller (branch 16077). That root copies
`file_map.children`, not the observation; an unmapped physical copy is absent.
No thin change to that detector can recover it without a second hidden disk
observation, mapped-node annotation/return-type interposition, or editing the
protected call. Thus it stays byte-identical here and moves to R2/R3 at J. J
passes the complete `prepared` envelope and makes the detector a **full delegate**
returning `prepared['duplicate_source_beacon_ids']`; preserve the outer shape
`{beacon_id: [{name, path, ...}, ...]}` and existing
`duplicate_source_beacon_id` warning type. U05's `duplicate_ids` verdict over
shared physical records supplies this projection, including unmapped copies;
do not recount mapped notes or reimplement `_duplicate_beacon_ids`.
Witness: `test_r2_duplicate_evidence_includes_unmapped_physical_copy`.

> R3 acn refresh_folder_cartographer_sync and srv _carto_task_terminal_payload → aggregate_beacon_outcomes/beacon_job_outcome, retain all file failures and historic events, correct partial status (do not patch obsolete ww path as the solution).

| Protected symbol / input | Component call and fields to copy | Branch behavior still outstanding / joined witness |
|---|---|---|
| acn `refresh_folder_cartographer_sync`; srv `_carto_task_terminal_payload`/`_start_carto_refresh_job`; one final structured outcome for **every** attempted file, new-file map/read failure included | `aggregate = support.aggregate_beacon_outcomes(files)` then `support.beacon_job_outcome(aggregate, infrastructure_error=run_wide_error, cancelled=cancelled)`. Copy entire returned data: `files` with full findings/history/check targets, `findings` (200 preview), `findings_total`, `findings_by_code`, `findings_truncated`, full `warnings` + exact `warnings_total`/`warnings_by_type`/`warnings_truncated`, full `events`, `files_changed`, `changed`, `summary_line`, `status`, `successful`, `failed`, `skipped`, `partial_success`, `success`. Strip only in-process `snapshot`/`analysis_inputs`, as aggregator does. | Branch terminal can say completed after local failures; main can fail whole job for one file. Do not use old ww cap/status as the rebase solution. Stored rows keep the planned outline only as a null-or-count/digest `reconcile_children` marker while finding anatomy and the public projection stay complete (D3-1). `test_r2_r3_folder_clean_corrupt_unreadable_and_new_files`, `test_aggregation_complete_cap_status_and_committed_phase8_failure`. |

> R4 srv _run_carto_bulk_visible_apply_job → accumulate node and follow-up refresh findings; satisfied noops can refresh; failed unresolved writes retain fail-closed behavior, mixed outcomes honest.

| Protected symbol / input | Component call and fields to copy | Branch behavior still outstanding / joined witness |
|---|---|---|
| srv `_run_carto_bulk_visible_apply_job` and `_start_carto_bulk_visible_apply_job`; existing claim, each file's ordered `summary.node_outcomes`, actual successful updates and distinct `nodes_satisfied`, R2 follow-up refresh outcome | Retain all node data as historic `node_outcomes`, one final R2 finding set per touched file (not per node), follow-up refresh warnings, repair events and errors. Feed all file outcomes through R3. R7-classified satisfied noops may trigger refresh; an unsatisfied noop/rejected write never does. Reuse the already prepared final file observation for the follow-up refresh where it is the same final hash, not 533 full analyses. | Allowed inner loop now adds data/counter ONLY: its existing `errors`, `nodes_skipped`, `node_updates` and returned successful count/refresh trigger remain unchanged. Protected job still drops refresh warnings and marks mixed node errors failed. `test_r4_allowed_bulk_projection_keeps_failclosed_trigger_and_all_node_data`, `test_r4_r7_joined_bulk_keeps_refresh_warning_and_partial_success`. |

> R5 mgp run_markdown_generate_pipeline → feed phase6 generated bytes into U08 pure preparation, publish ONCE only after all beacon/EOF phases; remove its early raw source write and intermediate mutation calls, preserve parent claim/no new Q3 conflict.

| Protected symbol / input | Component call and fields to copy | Branch behavior still outstanding / joined witness |
|---|---|---|
| mgp `run_markdown_generate_pipeline`; on main retain `_render_markdown_from_tree` and the one `_write_generated_markdown` dispatcher. Existing generation parent claim, target observed BEFORE phase6 disk mutation (or expected absence), generated phase6 bytes and same live heading forest | Under that parent claim call `prepare_generated_markdown_beacons(generated_bytes, source_forest, previous_snapshot, source_exists=...)`, then `publish_generated_markdown_beacons(prepared['publication_plan'])` once. Do not also publish individual heading/dedup/EOF mutations. Copy `outcomes`, `findings`, `omissions`, `relocations`, `analysis_error`, publication result and policy **separately**. No nested lease/new Q3 conflict. | Branch phase6 still writes beacon-free text at mgp 427–429, before phase7. Late phase7 cannot recover disk-only beacons already erased. `test_r5_r6_one_publication_and_phase8_failure_do_not_erase_disk`; U08 `test_beacon_markdown_publish.py` crash/CAS tests remain component evidence, not an activated gesture. |

Copy-ready U08 R5 join:
1. Under the existing generation claim, observe the target snapshot **before** the protected early write (or explicit expected absence for a new target).
2. Produce phase-6 generated bytes in memory as today, pass those bytes, the same live heading forest and that earlier snapshot to `prepare_generated_markdown_beacons(generated_bytes, source_forest, previous_snapshot, source_exists=...)`.
3. Call `publish_generated_markdown_beacons(prepared['publication_plan'])` once; do not then run separate per-heading/dedup/EOF source publications. Preserve the parent generation claim through commit return; **no lease nesting / no new Q3 conflict**.
4. Propagate `outcomes`, `findings`, `omissions`, `relocations`, `analysis_error` and publication evidence separately. Today’s phase-7 warning projection already exposes them on its late adapter path; J’s joined path must keep them without counting every diagnostic as a failed heading.
5. Treat committed disk outcome separately from phase-8 refresh outcome/job status (R6); retarget and execute U09’s joined witnesses on the real protected path at rebase. U08’s component crash tests are not those joined tests.

Branch residual: mgp `run_markdown_generate_pipeline` still writes phase-6 beacon-free text at final 427–429 before calling phase 7; whole-F12+3 old-or-new publication is therefore outstanding until J applies R5. Protected phase 7 currently counts every warning as a skipped beacon and its results count now includes failed heading outcomes; J must separate diagnostic rows, failed headings, prepared state and committed state.

> R6 mgp phase8 and job terminal → separate disk committed from map refreshed, merge findings/warnings; a phase8 failure reports committed disk plus failed refresh, not silent complete or fictitious rollback.

| Protected symbol / input | Component call and fields to copy | Branch behavior still outstanding / joined witness |
|---|---|---|
| mgp pipeline phase8; main `_run_phase8_refresh_inline`, `_write_generated_markdown`; branch `_launch_phase8_refresh_via_authority`; parent job terminal, actual publication result plus phase8 outcome | R3 aggregate publication files, then `support.beacon_job_outcome(aggregate, phase8=phase8)`; keep `publication` and `phase8` separate. `phase8.files` must carry COMPLETE final per-file findings (including [] for clean), not its capped top preview. These replace that file's prior current finding set; historic publication/omission/relocation evidence stays. Copy status/counts, exact totals, full detail, `files_changed`, `changed` and phase8 errors/warnings. | Branch launch failure can remain log-only, main phase8 warnings can disappear. Failed refresh after commit is useful disk work + warning/failed refresh, not rollback; only run-wide infrastructure/cancel or no useful outcome is `failed`. `test_r5_r6_one_publication_and_phase8_failure_do_not_erase_disk`, `test_phase8_current_findings_replace_old_version_without_losing_historic_events`. |

> R7 acn update_beacon_from_node/_apply_beacon_from_node_data and single-F12 prepare gate → only already_satisfied/already_absent count as satisfied success; unknown identity and rejected edit remain failures with reason. No wholesale noop-success change.

| Protected symbol / input | Component call and fields to copy | Branch behavior still outstanding / joined witness |
|---|---|---|
| acn `update_beacon_from_node` / main `_apply_beacon_from_node_data`, srv `_handle_refresh_file_node` single-F12 prepare gate (its local `gate`, wired as `preparation_gate=gate` into `_start_carto_refresh_job`; the identical-looking wirings in `_handle_materialize_markdown_file` and `_handle_bulk_materialize_markdown` gate materialization results and are not R7's); actual legacy writer result, same source/node context | `classified = support.beacon_legacy_outcome(writer_result)` at the existing classification point. It delegates evidence projection to U06, normalizes the existing safe-absence wire synonyms to `already_absent`, and recognizes ONLY `already_satisfied`/`already_absent` without an error. Copy original fields plus `noop_reason`, `satisfied`, `writer_success`, `success`, `findings`/candidates/diagnostics. The gate uses classified success; never overwrite it again with blanket noop-failure, and never map all noops to success. | Protected dispatcher/gate remain fail-closed on every noop here. The allowed bulk seam keeps original success while exposing this classification as additive data. `test_r7_only_explicit_satisfied_noops_allow_followup_refresh`, `test_r7_interrupted_delete_rerun_is_satisfied_but_unknown_labels_are_not`. |

## Current protected-definition measurements

Re-measured statically from NEXUS `e377a4a` plus the G5/C3 working bytes,
including all seven previously drifted acn coordinates, with
`tests/test_cut_2b_fences.definition_record`: source text is
parsed directly, with no product import. Ranges are coordinates, never identity;
the body hash is SHA-256 of the exact UTF-8 source lines from the first body
statement through `end_lineno`, including indentation and newlines, excluding
signature and decorators. Re-measure here with the same edit that moves a body
this table names, and re-measure before concluding that a byte differs.

| Protected qualified symbol | def–end | body SHA-256 |
|---|---|---|
| acn._prepare_carto_source_file | 6375–6396 | `8390984d91c743b69c37cc10ec032d9d73fd732b6bf48c1049954b55fac472ed` |
| acn._plan_carto_file_refresh | 6485–6654 | `422712ed181f6faef0897840764ea9883b7249086b904e35fbd8dcc9f0588d03` |
| acn.WorkFlowyClientNexus.refresh_file_node_beacons | 16843–17657 | `ec1ad981e4c134669a4160f5baa22a3c29a9e4b92c8e84b3cb9b525e7493b293` |
| acn.WorkFlowyClientNexus.refresh_folder_cartographer_sync | 17809–18711 | `025e3871f5134e8a31a2c12dd8546a8e2978f71098da8803e03fd46283c9201e` |
| acn._detect_duplicate_source_beacon_ids | 5391–5395 | `0117b74115bea889b1f212c4b81a368bad45b1098fad7dcf0ad26c8c38f0645f` |
| acn._apply_beacon_from_node_data | 7584–7663 | `81bf3f9504b52481caf696a8aedc332e240000aea5342fee0604af349cbbb901` |
| acn.WorkFlowyClientNexus.update_beacon_from_node | 17666–17709 | `771a92a48c25ae5e17161162bff92de82fde3cb827c04bce8c76fcc613c5e934` |
| srv._carto_task_terminal_payload | 11383–11482 | `b83dbaeed124aeb28213f966d5b7dbcd7896943f8a35a20c51692f8f0c4c13fd` |
| srv._handle_refresh_file_node | 3071–3248 | `298396170cd2f0ccedb81b69f47ff07367fd70cca98a7358e071dfdd884a05ad` |
| srv._carto_terminal_payload_data | 11491–11561 | `ba1ad546f43722ee4b110813b0a1e9ee3e8e39af39781db151166d4f3a7329bb` |
| srv._start_carto_refresh_job | 11700–11782 | `6163cc48f12e314a4994f19dfa21dc3aba25ee7cf3fe924507520e505776f308` |
| srv._run_carto_bulk_visible_apply_job | 13059–13559 | `b99902a806e54afb919d771189406e5c649b907f44b9657a5713b2fde96952ed` |
| srv._start_carto_bulk_visible_apply_job | 13569–13721 | `69cc0e0ba04d532a70b6c524bd75587c35e8e84c3c54bfaab53fc22757e355f1` |
| srv._bulk_apply_per_file_inner_loop | 12626–12814 | `ba91aaae42681ce65e0c96af2ceb33e5feee25eda927d5538b14ece6b5ca4779` |
| mgp._run_phase8_refresh_inline | 437–506 | `cacc83da93917cd07c5d96a1b417dbff9a2dbfcb8aa2a6b940e4f592504feb61` |
| mgp._write_generated_markdown | 384–421 | `927f756d7108bfdd3790bdf07abd73265091672af371882f62b6ba17c9789455` |
| mgp.run_markdown_generate_pipeline | 516–882 | `df28947fd08ab5ff602be56872fc9157c4fcdc61e06550333a40f96c5d7e392b` |
| support.prepare_beacon_source (NE35OC companion) | 75–195 | `248fcc81adedc58cbb7b123abf079d35db4fae791b20217dcec76b0028687cfc` |

The J-delivery values this table replaced are history, as are the
`u09_symbols.current` rows (original cf77d0d-time evidence, including the two
server rows — bulk inner loop and legacy tool) and `seam_transfer.base/current`
equality (transfer time). Current truth is this table with
`protected.versions.merged`, `program_symbols.current`, the independent pins and
the implementation Result; the changed detector is pinned there, not by
rewriting the transfer record. The component's amended current record is
`program_symbols.current`; its U09 row remains historical.

## Historical rebase coordinates and byte hashes

Measured main: **`6df6b70cf4c98f402a57411fa12060f7ea845b8b`**. Re-measured when
main `9fbf77753a35c0ebaceea98efa51f67534b22f7e` was merged into this branch:
every main-column body hash below is unchanged, and every protected body in the
merged tree equals it (merged ranges: the `merged` versions in
`tests/beacon_robustness_u09_manifest.json`). Branch coordinates
are the U09 W source shape (protected bytes unchanged from `d8da543` and
`a345dc7`). Hash is SHA-256 of exact UTF-8 full source lines from the first body
statement through `end_lineno`, including indentation/newline, excluding the
signature/decorators. The repo-local mechanical register additionally carries
definition and AST hashes. `absent` is explicit, not an unmeasured body.

| Protected qualified symbol | Branch range · body SHA-256 | Main range · body SHA-256 |
|---|---|---|
| srv._handle_refresh_file_node | 3155–3338 `e7e38c316e0953e96412ec05f52fa807dd0cb320a44f86758c91dd5c66eebe40` | 3236–3423 `8aaecc2e62c629a4d417c965b1731e48d0c977aff533bd418c4109bc913e8a58` |
| srv._start_carto_refresh_job | 10999–11137 `2f7ac8ab3ad363faa126f4ff9413cb56cb2bcaea9c421aa366c97bb2093b94f6` | 11652–11734 `6163cc48f12e314a4994f19dfa21dc3aba25ee7cf3fe924507520e505776f308` |
| srv._start_carto_bulk_visible_apply_job | 12547–12808 `38eb608762b1ed6964342eb5ea3db4ce83ad28bc03789640cb408461f89b760d` | 13252–13403 `4b637d0799b5383fe21b3d9960720c7ef92a6d2dc86745371bad510f9bf81013` |
| srv._run_carto_bulk_visible_apply_job | 12284–12537 `feb560575b45669227e808e5658587928b27cbd09ab751cc30b357eb2c2875a5` | 12945–13242 `f98fd065744c7f380bb69a3f3298af5bbe0dde644e09e5def107c434af4ce877` |
| srv._carto_task_terminal_payload | absent | 11438–11466 `0afb389a1f6cde5fb7f10e09861c4d8c4e98cf3c5e2f929b57615f38c667a13b` |
| acn.WorkFlowyClientNexus.refresh_file_node_beacons | 15509–16647 `a3b1e747a5b6ce0ee69dea949a2143f12d2e3739d6ce52c40f39d538c03ddaf6` | 17058–17636 `003e3ee66aeeb2b7b1b16d3cc43062885ba54a2485d0c5f8c2c9d86c8197069c` |
| acn.WorkFlowyClientNexus.update_beacon_from_node | 16655–16812 `68b07ad03e43ca9464c2b239f2ba77c59bebe0e2e1d04c2e219bc404dd897f7a` | 17645–17682 `07d9566c296a3e096804606db2b6c1033ecd7e91cf8ad304211d236599128814` |
| acn._apply_beacon_from_node_data | absent | 7807–7881 `ac81d4fe9e69efefc01fd8879f3bad63c3cd680eced8426914621553884c9f0b` |
| acn._prepare_carto_source_file | absent | 6669–6714 `7d23d7851d7624e676542a89e43102f45ca584291c44efc25718d466dd9a7158` |
| acn._plan_carto_file_refresh | absent | 6803–6956 `c1345de645adf3733e4a097942a038ce3321492f6764f1a198f9849228d7803e` |
| acn.WorkFlowyClientNexus.refresh_folder_cartographer_sync | 16912–18618 `5ee87e698312487fb571048e19997ad747844c18d88c1cb91feea0f012639911` | 17801–18476 `51ccfcd7864a73305ae8ca6f7637369230e767b40329c3a4c27c1cfff17e869b` |
| mgp.run_markdown_generate_pipeline | 255–716 `dcedb7fbaa70fef9319bb03b52aeb35aaf6e953d9dafa06f4a87f6043bb7e071` | 638–979 `41ffe4b3cfd6bf259188eed7dae9405dcb08bec84ee79d00d5e1f4776c7c7d5f`; CUT-2B: 441–785 `c87488b481a8b055588acddd38d8626a456a5a0a9a71bc6b89e415ed5a2454e4`; CF-44 |
| mgp._launch_phase8_refresh_via_authority | 744–838 `aeeb46d0544dfdc1fc68046810dd12d4a50442bff591eb7d2bbc53b961c11828` | 1009–1103 `aeeb46d0544dfdc1fc68046810dd12d4a50442bff591eb7d2bbc53b961c11828`; CUT-2B: absent; worker/adoption retirement |
| mgp._write_generated_markdown | absent | 568–595 `b3db34e382cd683ac29a2fc85597fca39ff6228c26da21e4aac8054824fe81e7` |
| mgp._run_phase8_refresh_inline | absent | 611–628 `a3bd6e0f45b42b59a5edad6bfb3529a10233f697407ea958dba0f9f7ed4db945` |
| cjg.launch_carto_refresh_job | 4451–4810 `db47419ee7bc78933ec983b44bfc18e441edb3ed43a0875786f02161a2ee0775` | 4668–4864 `782e1ba0832e4417d872e35458fb6b7efe3912e30f5c7595ab1f34a42817db98` |
| ww.main | 279–1300 `ed4bd7fbe0b8e233daa8c450fa33f7e397b2afbff82e85e3df754090a857a9d4` | 280–1302 `4908ac04bd70cbd41e235ff5c9a9fa85df9b58ba577ad0e83adc3740da98c2da`; CUT-2B: absent; worker/adoption retirement |

CUT-2B historically re-anchored only the two retired worker/adoption bodies and
the CF-44 pipeline exception path. Every hash in this section is frozen history
from the commit it names; current bodies are measured only in the table above.

### Allowed seams and transfer measurement

| Seam | Branch base → U09 range/body hash | Main range/body hash and action |
|---|---|---|
| srv._bulk_apply_per_file_inner_loop | 11942–12097 `773dea6d63b7a62601326788e8e0f1a070619a059e3bd39d6ead6b1f09dc6e23` → 11942–12097 `16d7813e243dead472a40e3109b5dacfea9846f8e41968da94d7eb209221003e` | 12570–12723 `db444211b739be78723d3932318c354e0935c0e3f8bc8fcabb01bfcf8dd9ed8d`; main c8f1699 changed candidate preparation to `run_carto_cpu`, added keyword-only `flush_artifacts`/`log_result`, and `_bulk_result_log_text`. Reapply ONLY additive result projection immediately after `client.update_beacon_from_node`, before result logging; keep main callbacks/artifact order. Applied at the merge of `9fbf777`. |
| srv.update_beacon_from_node (legacy tool, NOT acn protected method) | 44427–44471 `25b1c95411e3409f323a09bc0dfff2e1ae1347fcc91c1e56e93e2b2317b2d9d4` → 44428–44479 `0b71c7249607e6157009524f23467b8da47e0bb8873c1254904d347dacc6136f` | 45250–45294 `25b1c95411e3409f323a09bc0dfff2e1ae1347fcc91c1e56e93e2b2317b2d9d4`; same original body. Keep lease/rate limiter/finally; copy U06 post-writer `project_sync_result` call only. Auto-merged at the merge of `9fbf777`: the merged body is the U09 hash above. |
| acn._detect_duplicate_source_beacon_ids, TRANSFERRED R2/R3 | 5382–5427, unchanged `494b3e18bb8c28c75553b5c233ba81d97a72eb0830f8f0d4aa65b7e65bedb833` | 5654–5699, same body/hash. Sole protected caller drops observation before detector; transfer rationale and exact delegation above. |

U06 `_compute_beacon_write_plan` (acn, module level), not a new R row: main's
body equals `a345dc7`'s (body SHA-256 `da64ffa77d9813c8…`), so the branch's
U06 body merges cleanly and is kept **whole**; on any textual conflict keep the
branch's entire body, never main's side, and re-verify by hash (BR-27). At the
merge of main `9fbf777` it auto-merged; the merged body SHA-256 is the branch's
`5fdd0d3db7759fe6…`. Retain main's later dependencies/build-hook changes when
merging packaging, not one side wholesale.

BR-07 informational: MCI rewrote both server Markdown materializers on main to write through `_write_generated_markdown(operation="write")`; post-merge attention, with the Ledger row remaining the authority; no U09 code change.

## Activation acceptance

Run the retargeted R1–R7 scenarios, ordinary-FILE BR-09 with retention, the
four-shape × four-operation matrix, front acknowledgements, inventories and
governor/finalization fences. The Conductor then runs full tier on the committed tree and
`python3 scripts/run_tests.py --verify --tier full`. Report the real function
under test, claim context, raw bytes/mtime, retained UUIDs, exact counts and
actual publication/refresh outcomes. No component-only pass substitutes for
that joined verification. J's focused dual audit uses the practical bar; U09
does not accept its own work or claim deployment.
