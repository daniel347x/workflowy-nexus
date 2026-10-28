# S06 Pre-Edit Reachability Inventory (BINDING — §3.4 / H1 / H2)

Baseline commit: `adf58e0` (S03-A sealed). Generated from the repository-wide
AST/text audit (`C:\temp\s06_audit.txt` + targeted follow-up reads of every
candidate region). Classes per S06 H1: `dead-unreachable` /
`retired-live-feature` / `replace-with-narrow-predicate` / `retain-active` /
`documentation-test-beacon`.

This inventory is the auditor's 1:1 checklist against the diff (H2): every
DELETE-classified entry shows its deletion; every REPLACE entry maps to exactly
one shipped replacement; every RETAIN entry carries its reason; every
substantive production deletion appears here.

---

## A. WEAVE worker dead modes (Gate S06-A)

| # | Symbol / string | File : enclosing function | Live callers | Class | Disposition |
|---|---|---|---|---|---|
| A1 | `--mode enchanted` / `--mode direct` argparse choices | `src/workflowy_mcp/weave_worker.py` : `main` | None — the only production launch authority (`carto_job_governance._spawn_governed_worker`) emits `carto_refresh` (2 sites: 4114, 4208) and `markdown_generate` (server.py:9620); no console script in pyproject.toml; no MCP tool; no test | dead-unreachable | DELETE choices; restrict to `carto_refresh`, `markdown_generate`. Current disposition: removed with the worker in CUT-2B. |
| A2 | `--nexus-tag` / `--json-file` / `--parent-id` / `--import-policy` worker args | `weave_worker.py` : `main` parser | None (consumed only by the dead modes) | dead-unreachable | DELETE args. Current disposition: removed with the worker in CUT-2B. |
| A3 | `--dry-run` worker arg | `weave_worker.py` : `main` parser | Parsed but consumed ONLY by the dead LAB branch (`nexus_weave_enchanted`/`bulk_import_from_file`); surviving modes hardcode `dry_run=False` internally | dead-unreachable | DELETE arg **AND** the launcher emission `"--dry-run", "false"` in `carto_job_governance._spawn_governed_worker` (mechanically forced: argparse would otherwise reject every governed launch). Current disposition: removed with the worker in CUT-2B. |
| A4 | `NEXUS_RUNS_BASE` worker-side lookup + requirement | `weave_worker.py` : `main` (~424-432) | Only the dead modes required it | dead-unreachable | DELETE lookup + error branch |
| A5 | `NEXUS_RUNS_BASE` launcher producer `env.setdefault(...)` | `src/workflowy_mcp/server.py` : `_carto_account_scope_and_env` (:9405) | Dead once A4 is deleted (W1 amendment) | dead-unreachable | DELETE setdefault + now-unused `get_nexus_runs_base_dir` import (server.py:98; no other server.py use) |
| A6 | `.weave.pid` resolution/write/cleanup | `weave_worker.py` : `main` (enchanted/direct branches + `pid_file` write block + LAB-branch unlink) | Only the dead modes | dead-unreachable | DELETE; remove `("workflowy_mcp.weave_worker:main","attribute.unlink","pid_file")` from `tests/test_carto_finalization_caller_closure.py` DELETION_CALLSITE_REGISTRY |
| A7 | LAB branch: `client.nexus_weave_enchanted(...)` / `client.bulk_import_from_file(...)` + `wkv1@detached-weave-lab` span | `weave_worker.py` : `main` tail | `WorkFlowyClient` has NO such methods (grep-verified across client/); manual invocation would fail | dead-unreachable | DELETE whole trailing try/except/finally + span beacon; surviving modes keep their own `finally: await client.close()` + heartbeat stop + claim finalization |
| A8 | Non-CARTO cache-bootstrap `elif/else` (warm-start log / self-refresh) | `weave_worker.py` : `main` (~544-562) | Unreachable once only CARTO modes survive (mode is always in `("carto_refresh","markdown_generate")`) | dead-unreachable | DELETE elif/else (H4); S02/S03-A lineage `if` branch preserved verbatim; phase_c fence `"Refreshing /nodes-export cache for WEAVE worker" not in source` stays green |
| A9 | Module docstring ENCHANTED/DIRECT usage prose; `WORKER BOUNDARY PLAN` two-worlds comment; `Starting WEAVE worker ... dry_run=` log | `weave_worker.py` top / `main` | — | documentation-test-beacon | REWRITE to governed CARTO worker wording (filename `weave_worker.py` retained per §4.1) |
| A10 | `get_nexus_runs_base_dir` / `NEXUS_RUNS_BASE` env override / CONFIG_REFERENCE row | `config.py:115,587`, `api_client_nexus.py:3810`, `CONFIG_REFERENCE.md:89` | LIVE non-worker configuration consumers (H5 scoping note) | retain-active | RETAIN untouched — S06 covers ONLY the worker lookup (A4) and launcher setdefault (A5) |

## B. JEWEL in `client/workflowy_move_reconcile.py` (Gate S06-B)

| # | Symbol / string | Enclosing function | Live callers | Class | Disposition |
|---|---|---|---|---|---|
| B1 | `update_jewel_with_uuid` (nested) + dynamic `nexus_json_tools` import + `transform_jewel` ops | `reconcile_tree` | Called only from CREATE step 3 (itself JEWEL) | dead-unreachable | DELETE function; `import importlib` + `import sys` removed (AST-verified no other use in module) |
| B2 | `jewel_file` local + `source_json.get('jewel_file')` + log interpolation | `reconcile_tree` input normalization | Feeds only B1 | dead-unreachable | DELETE local + read; historical JSON carrying the key is naturally ignored by the generic dict parser (§5.1); log line becomes jewel-free |
| B3 | `jewel_sync_ledger` + all 8 field sites (`created`/`fetched`/`jewel_updated`, dry-run triplet, `ledger_entry`, 2 appends) | `reconcile_tree` / `ensure_created` | Feeds only B4/B6 | dead-unreachable | DELETE; CREATE journal becomes `started, resumability_safe=false` → `completed, resumability_safe=true` (§5.3); `nonlocal create_count, jewel_sync_ledger` → `nonlocal create_count` |
| B4 | `jewel_sync_summary` computation + plan key + SUMMARY log lines | `build_plan` / `log_summary_and_close` | Reads B3 | dead-unreachable | DELETE block; plan keeps settings/counts/protected refs |
| B5 | `"completed_jewel_ok"` / `"completed_jewel_failed"` journal statuses | `ensure_created` | Only B3 context | dead-unreachable | DELETE (replaced by generic `completed` per B3) |
| B6 | Final JEWEL/ETHER guidance block + `nexus_weave_enchanted_async` rerun instructions + affected-node warnings | `reconcile_tree` tail (~1566-1604) | Reads B3 | dead-unreachable | DELETE block; `return log_summary_and_close()` retained |
| B7 | `"Detected bare source node list (no jewel_file metadata)"` message | `reconcile_tree` | — | documentation-test-beacon | REWORD: "Detected bare source node list (no document metadata)" |
| B8 | `protect_notes_emoji_delete_guard` local + `source_json.get(...)` + enable log + DELETE-loop skip + `_is_salvageable_notes_delete_guard` + `from .api_client_nexus import NOTES_MARKER_FIRST_CODEPOINTS` | `reconcile_tree` / DELETE phase | Producer is api_client_nexus (C4) — feature retired | retired-live-feature | DELETE all (§7); historical JSON carrying the flag is naturally ignored |
| B9 | ETHER vocabulary in retained comments/logs ("CREATE in ETHER", "preserved-in-ETHER", "ETHER edits", "remain in ETHER") | `reconcile_tree` (398, 411, 830, 886, 1290, 1403-1404) | — | documentation-test-beacon | REWORD to identity-seeding ambiguity protection / neutral Workflowy wording (§6.2); `explicitly_preserved_ids` behavior unchanged |
| B10 | `_log_to_file_helper` `jewel`/`jewelstorm` routing → `jewelstorm_debug.log` | `client/api_client_etch.py:52-53` | ZERO callers with those log_types (audit-verified) | dead-unreachable | DELETE branch; beacon comment loses `jewelstorm_debug.log` mention; active reconcile/etch/nexus logs untouched |
| B11 | `_infer_preview_prefix_from_path('enchanted_terrain.json')` | `client/api_client_etch.py:219-230` | LIVE caller `api_client_etch.py:1364` (export preview annotation; keys off user-chosen output filename) | retain-active | RETAIN whole function unchanged (§5.5: live historical-filename convenience, not dead WEAVE execution; removal would broaden S06) |
| B12 | ETHER prose in `nexus_map_codebase.py` (`reconcile_trees_cartographer` docstrings/comments, lines 3225-3557, 14458) and `nexus_helper.py:160` | — | Live Cartographer identity-seeding vocabulary ("ETHER tree" = existing Workflowy tree) | retain-active | RETAIN (out of scope: §3.2 targets reconcile_tree's JEWEL/ETHER guidance, not mapper vocabulary; H7 counterweight) |
| B13 | `_detect_duplicate_ether_beacon_ids` / `_detect_duplicate_source_beacon_ids` | `api_client_nexus.py` | LIVE: pre-refresh diagnostic + user-facing disk-duplicate warnings in `refresh_file_node_beacons` | retain-active | RETAIN unchanged (H6 named survivor; "ether" in name is historical, not dead code; no cosmetic rename) |

## C. Salvageable-subtrees retirement (Gate S06-C) — class `retired-live-feature` unless noted

| # | Symbol / string | File : enclosing function | Live callers | Disposition |
|---|---|---|---|---|
| C1 | `NotesSalvageContext` dataclass (+beacon) | `api_client_nexus.py` :3457-3480 | `refresh_file_node_beacons`, C7, C8 | DELETE class |
| C2 | `NOTES_MARKER_PREFIXES` + `NOTES_MARKER_FIRST_CODEPOINTS` (+beacon) | `api_client_nexus.py` :3342-3360 | C3, C5, C6, `workflowy_move_reconcile` B8 | DELETE both; cosmetic consumers switch to C12 |
| C3 | `_is_notes_name` (+beacon) | `api_client_nexus.py` :3362-3455 | parse-error promotion (C5), folder stash/delete-skip (C9/C10), collect/restore (C7/C8), `markdown_roundtrip._load_notes_name_predicate` (C14) | DELETE; promotion caller replaced by C13 |
| C4 | `protect_notes_emoji_delete_guard` producer (`source_json` key) + Notes-descendant `explicitly_preserved_ids` collector + `children_by_parent` + `_collect_notes_descendants` + `is_markdown_file` + Markdown opt-out comments | `refresh_file_node_beacons` :13216-13277 | feeds reconcile B8 | DELETE collector/guard/opt-out machinery; KEEP `original_ids_seen`, `carto_identity_seed_report`, and the `preserve_ids`-driven `explicitly_preserved_ids` population (identity ambiguity, H6) |
| C5 | Parse-error promotion condition `"parse error" in cname.casefold() and _is_notes_name(...)` | `refresh_file_node_beacons` :12994-13007 | — | replace-with-narrow-predicate → **C13** (`s801-inner1` span deleted) |
| C6 | `salvage_ctx` init + `_collect_notes_salvage_context_for_file(...)` call + `_restore_notes_for_file(...)` call + `result["notes_restore"]` | `refresh_file_node_beacons` :12590, 12866, 13659-13676 | C7/C8 | DELETE call sites + result key (zero external readers — consumer scan-verified) |
| C7 | `_collect_notes_salvage_context_for_file` (+beacon, 3 inner span beacons, Markdown opt-out) | `api_client_nexus.py` :15961-16213 | only C6 | DELETE method |
| C8 | `_restore_notes_for_file` (+beacon, `_ensure_salvaged_root` + inner span, `🧩 Notes (salvaged)` creation, Markdown opt-out) | `api_client_nexus.py` :16215-16544 | only C6 | DELETE method |
| C9 | Folder stash machinery: `stashed_notes_by_abs_path`, `notes_moved_to_root`, `_stash_notes_node`, root-level + per-file `_is_notes_name` scans + pre-refresh moves | `refresh_folder_cartographer_sync` :14420-14450, 14589-14613 | — | DELETE (§8.1); folder job performs fewer MOVE calls |
| C10 | Folder reattach: post-refresh re-export + `abs_to_node_id` + `notes_reattached`/`notes_unmatched` moves | `refresh_folder_cartographer_sync` :15845-15930 | — | DELETE (§8.1) |
| C11 | Missing-file deletion `_is_notes_name` skip (`kj4d-0003` span) | `refresh_folder_cartographer_sync` :15264-15272 | — | DELETE skip (§8.2); strict classifier (first-line `Path:` + abs candidate + non-placeholder basename + basename-in-name) is the sole guard; regression fixtures prove diagnostic/prose nodes never enter `file_nodes_info` |
| C12 | Cosmetic suffix stripping in path derivation (4 sites: `resolve_cartographer_path_from_node._segment_from_node_name` ×2, `refresh_folder_cartographer_sync._segment_from_node_name_for_path` ×2 + `_compute_folder_relpath` ×2 uses of `NOTES_MARKER_FIRST_CODEPOINTS`) | `api_client_nexus.py` :2887-2920, 14262-14280, 15486-15534 | live path behavior | replace-with-narrow-predicate → **new neutral constant `CARTOGRAPHER_COSMETIC_SUFFIX_FIRST_CODEPOINTS`** (same frozenset values; comment: cosmetic path/name normalization ONLY, §9.2 option 2); golden path fixtures must remain byte-equivalent |
| C13 | NEW narrow pure predicate `_is_cartographer_parse_error_sentinel(name)` | `api_client_nexus.py` (new, module-level) | C5 caller | Replacement for C5: `name.lstrip().startswith("⚠") and "parse error" in name.casefold()` — matches the verified Cartographer sentinel shapes (`⚠️ MD Parse Error`, `⚠️ Parse Error`, `⚠️ Parse Error (JS/TS)`, `⚠️ Parse Error (VUE…)` from nexus_map_codebase:2264,6079,7463,7576,7616,7631); never classifies arbitrary emoji-prefixed user nodes |
| C14 | `_load_notes_name_predicate` + `_NOTES_NAME_PREDICATE` + `_is_persistent_notes_subtree` (+2 beacons) | `markdown_roundtrip.py` :566-630 | ZERO live callers — the 3 references (:689, :1051, :1097) are all "do NOT filter here" COMMENTS | DELETE helpers; rewrite the 3 comments to drop the references (behavior unchanged: no filtering, F12+3 emits emoji-prefixed nodes) |
| C15 | Folder result fields `notes_stashed` / `notes_moved_to_root` / `notes_reattached` / `notes_unmatched` + completion-log interpolation | `refresh_folder_cartographer_sync` return + log :15954-15957, 16525-16541 | zero external readers (consumer scan-verified; weave_worker reads `refreshed_file_nodes`/`warnings` only) | DELETE fields (§8.3; no zero-valued shims) |
| C16 | Folder refresh docstring "NEW (Jan 2026): stashes…" bullet + "GEM/JEWEL/WEAVE" closer; `refresh_file_node_beacons` docstring CORE V1 KEEPER comment | `api_client_nexus.py` :14090-14098, 12527-12531 | — | REWRITE (§11/H4) |
| C17 | `ncv1@f12-refresh-and-notes` span role "…persistent notes-salvage system" + "CORE V1 KEEPER … robust Notes and diagnostic salvage" comment | `api_client_nexus.py` :12527-12537 | — | REWORD to F12-refresh-only keeper claim (§9.3); span id + boundaries retained |
| C18 | `ra-notes-salvage` slice label on RETAINED beacon blocks | `api_client_nexus.py` (29 blocks incl. path helpers, nexusignore helpers, `_segment_from_node_name` spans, both refresh functions), `nexus_map_codebase.py` (15 EMOJI_*/TRIDENT spans), `beacon_mutation_contract.py` (`strip_leading_decoration_symbols`) | — | REMOVE label only (§12; `ra-notes` untouched — only `ra-notes-salvage` is denied); deleted symbols' blocks vanish with C1-C3/C7-C9 |
| C19 | `tests/test_carto_dry_run_byte_purity.py` monkeypatch of `_collect_notes_salvage_context_for_file` | tests :46-60 | — | documentation-test-beacon → DELETE monkeypatch (H3; refresh no longer calls the method) |

## D. Explicit non-goals verified during audit (STOP conditions checked — none triggered)

- `nexus_map_codebase.py` `__main__` CLI (`--parent-id`, `--nexus-tag`, writes
  `enchanted_terrain.json`): standalone historical mapper CLI, NOT a worker
  mode or launcher → retain-active (§5.5: do not broaden S06).
- `markdown_roundtrip.py:1177` CLI help mentioning `enchanted_terrain.json`:
  prose → retain-active.
- `nexus_helper.py:160` / `server.py:20543` / `api_client_nexus.py:5178`
  "ETHER"/"WHETHER" prose: false-positive substring hits → retain-active.
- `LICENSE`, `workflowy-glimpse-cache/katex/LICENSE.txt` "whether": irrelevant.
- `server.py.stage2-corrupted-python-…` stray backup: UNTRACKED + gitignored
  (`*.stage2-corrupted-*`); NOT deleted (invariant 20 — no opportunistic
  cleanup) and cannot enter the commit.
- weave_worker.py was retained at S06 and is deleted whole by CUT-2B; no worker entry remains.
- `.weave_journal.json` diagnostic writer + `log_weave_entry_fn`: retained
  (§5.3/§6 invariant 6) — only JEWEL statuses/summary/guidance go.
- `reconcile_tree` + all five phases + `original_ids_seen` +
  `carto_identity_seed_report.preserve_ids` + `explicitly_preserved_ids` +
  truncation/threshold/cancellation guardrails: retained (H6).
- The S06-retained worker adoption/bootstrap/finalization machinery is deleted by CUT-2B. Its tests retire or convert to the explicitly retained daemon-task, COW and in-memory ownership witnesses.

## E. Mechanically-forced edits outside the three primary files (H4/H5 manifest)

1. `server.py` — remove `env.setdefault("NEXUS_RUNS_BASE", ...)` + unused
   `get_nexus_runs_base_dir` import (A5/W1).
2. `carto_job_governance.py` — remove `"--dry-run", "false"` from
   `_spawn_governed_worker`'s launch command (A3-forced). Current disposition: removed with the worker in CUT-2B.
3. `tests/test_carto_finalization_caller_closure.py` — remove the
   `weave_worker:main` `attribute.unlink` `pid_file` registry entry (A6). Current disposition: removed with the worker in CUT-2B.
4. `tests/test_carto_dry_run_byte_purity.py` — remove the salvage monkeypatch
   (C19).
5. `markdown_roundtrip.py`, `nexus_map_codebase.py`,
   `beacon_mutation_contract.py` — C14/C18 (root modules → editable reinstall
   before pytest per repo warning).
6. `README.md` — add the §11 concise retirement note (no existing salvage
   claims found in source-controlled docs; audit-verified).
7. Workflowy-hosted reference ("Exception — salvageable subtrees" F12+1 doc
   node): NOT source-controlled → listed as a manual follow-up for Dan in the
   S06 result (§11).

---

## F. Amendment round 1 additions (audit A1–A7 dispositions)

Recorded per the independent audit's AMEND verdict; each item names its class
and shipped disposition.

| # | Symbol / string | File : enclosing function | Class | Disposition |
|---|---|---|---|---|
| F1 | `_is_cartographer_parse_error_sentinel` overbreadth (accepted `⚠️` + any "parse error" prose) | `api_client_nexus.py` | replace-with-narrow-predicate (tightened) | Producer-exact grammar: the five `_warn_name` base forms (`_CARTOGRAPHER_PARSE_ERROR_SENTINEL_NAMES`) + timestamp-suffix regex (`_WARN_NAME_TIMESTAMP_SUFFIX_RE`); same-emoji user headings rejected (direct + integrated negative tests) |
| F2 | FUTURE DESIGN docstring salvage-algorithm contract | `beacon_obtain_code_snippet.py` module docstring | retired-live-feature (missed in round 1) | Rewritten as a historical note: per-file refresh shipped as F12+1; Notes-salvage RETIRED in S06 |
| F3 | `NotesMapping` @dataclass (+beacon) — zero callers | `beacon_obtain_code_snippet.py` :100-115 | retired-live-feature scaffold | DELETED (fence denylist extended) |
| F4 | `compute_sha1` — zero callers, justified only by the retired refresh design | `beacon_obtain_code_snippet.py` :123-134 | retired-live-feature scaffold | DELETED (+ now-unused `hashlib`/`dataclass` imports; fence denylist extended) |
| F5 | `bocsv1@refresh-scaffold-boundary` span + SPLIT BOUNDARY comment | `beacon_obtain_code_snippet.py` :89-96 | retired-live-feature scaffold | DELETED (fence denylist extended) |
| F6 | "We emit a *salvageable* warning node…" | `nexus_map_codebase._prepend_duplicate_beacon_id_warning_nodes` docstring | documentation-test-beacon | REWORDED: "explicit disk-derived diagnostic warning node" |
| F7 | `wkv1@file-boundary` beacon role "…vs advanced weave lab" + `main` "SPLIT BOUNDARY …both core and lab" comment | `weave_worker.py` | documentation-test-beacon | REWORDED to governed-CARTO-worker wording. Current disposition: removed with the worker in CUT-2B. |
| F8 | "Children index … (Notes stashing)" / "Collect file nodes first so we can stash Notes…" comments; `notes_prefixes` aliases (3 sites) | `api_client_nexus.py` folder refresh + cosmetic sites | documentation-test-beacon | REWORDED to real purposes; aliases renamed `cosmetic_prefixes` (vestigial relpath-token site deleted with its `r800-inner3` span) |
| F9 | Stranded `node_note` local (deletion-created, F841) | `workflowy_move_reconcile.py` DELETE loop | H4 mechanically forced | DELETED |
| F10 | `test_s03_fence_allowlists…` line-oriented + quote-bypassable | `tests/test_s06_s03b_inventory.py` | test-honesty defect | REWRITTEN: AST scanner `_scan_allowlist_source` + injected single-line/multiline/dict-key/quote/nested-path fixtures + legit function-scoped negative |
| F11 | Ancestor/MOVE/REORDER/folder-emoji-file/folder-create test gaps | S06 test files | test-contract incompleteness | Real ancestor-propagation test (preserve child, parent survives via walk); `test_parent_changing_move_still_applies`; `test_same_parent_sibling_reorder_still_applies`; `📝 missing3.py` strict-classifier deletion boundary; `test_disk_create_parity_new_file_and_intermediate_folder` (🐍+ƒ child, 📂 sub, parentage) |
| F12 | Cosmetic-site test loose (`uses >= 4`) | `tests/test_s06_path_behavior_parity.py` | test-contract incompleteness | Exact AST census (1 store + 5 loads), banned-alias sweep, 3-region coverage |
| F13 | New S06 beacons used legacy `role=` | `api_client_nexus.py` (2 beacons) | beacon metadata convention | Migrated to `comment=` (role omitted) |
| F14 | Result-report net-reduction figures were touched-line totals | `ap:ECHXN4` result note | report fidelity | Corrected to numstat nets (−1000 / −238 / −179) in the amended result addendum |

*Amendment-round disposition of the A2 missed-scaffold class: `beacon_obtain_code_snippet.py` was absent from the round-1 inventory — an H2 completeness failure, now closed; the fence denies `NotesMapping`, `compute_sha1`, and `bocsv1@refresh-scaffold-boundary` permanently.*
