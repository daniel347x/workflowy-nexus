# JavaScript tests (tests/js/)

The repo's dependency-free JavaScript harnesses live here:

- `test_dock_source_view.js` — docked Markdown Source-view + find-in-source.
- `test_md_render_fixture.js` — shared GlimpseMd API and real dock delegates against immutable pre-extraction HTML/search-state data using the extension's marked/KaTeX; CSS migration, manifest order and single-owner checks.
- `test_md_row_list_fixture.js` — shared row painter, real dock delegate and canonical-name alias against immutable pre-move DOM/event/reference/CSS data; one owner, guides, scope class and load order.
- `test_lens_modal_parity.js` — LensSpec modal carrier/timeout parity.
- `test_carto_heartbeat_widget.js` — compact CARTO heartbeat triage and old-payload compatibility.
- `test_carto_jobs_twenty_tabs.js` — recorded daemon stream and 20 shipped-code page VMs; received/parsed bytes, list rebuilds, badge writes, visibility polling and legacy compatibility.
- `test_carto_jobs_details.js` — one targeted request per click, complete problem paging, Run first, evidence-version notice and the existing copy/cancel/clear controls.
- `test_carto_jobs_feedback.js` — CF-151 branches 1–3: launch reason, visible display failure with re-poll, and unknown mutation outcome with a retained late-reply hook.
- `test_saved_tabs_export_import.js` — Saved Tabs (Tier-0) export/import pure functions: the one-payload-format assembly (`{alias,title,spec}` — no page url since U02; older url-carrying payloads still parse) byte-diffed against the 10 harvested tab specs, the 4-way row classification, tolerant import parse, label derivation, and the exact-match group_id pre-validation.
- `test_saved_tabs_tier1.js` — Saved Tabs (Tier-1) file + Anchor-Dock routes: timestamped export filename, dock-run name identification (emoji/AP-ID strip, prefix match), created-date recency pick, tolerant (```json fence OR bare JSON, E3) payload extraction from composed markdown, last-used-dock localStorage round-trip, and static one-serializer / read-only-dock-route wiring locks.
- `test_f12_force_override_popup.js` — U5 fail-closed override row: `dispatchF12RefreshAction` carries `force:true` only on an explicit force; `executeF12PopupAction`'s two-step in-popup confirm arms before dispatching; source-shape locks for the row's key/alwaysLast/confirmFirst wiring and the widened 0-9 key gates. Also drives the real key listeners (`initializeUuidHoverHelper`: one physical key is one press — the arming 0's keyup never confirms, repeats never act, a sub-menu never takes the opening key's release, keyup alone still works), `showF12ActionPopup`'s late bulk verdicts (never written into a sub-menu or a newer menu on the same node), and the F9 duplicate-heading picker (digit and click dispatch the chosen file/line before the state is cleared).
- `test_slice_group_durable_read.js` — U7 durable read for slice-group state: the `get_slice_group_config` branch answers `expand_groups`/`active_group` from chrome.storage via the SW `get_expand_groups` bridge kind (behavioral round trip with a getter-trapped stale mirror), the sync→local migration fallback rides the one merge authority, bridge failure is a loud `success:false` with no mirror fallback, the sync-blob vocab still comes from the mirror, plus §14 wiring locks (no third copy, write protocol unchanged).
- `test_saved_tabs_drive.js` — Saved Tabs (Tier-2, unit T2A; U01): the SW multi-tab drive (sequential create→await-ready→deliver→settle state machine with injected caps; U02: every tab opens at plain `https://workflowy.com/` — the saved page url is ignored, never admitted or navigated to; immediate cancel — the per-drive cancel signal raced against the delay, the ready wait and the apply wait, with waiter teardown; supersede instead of a one-drive refusal; window reuse across drives; the configurable inter-tab delay re-read per gap; the eviction marker + restart abort report; origin-death stop-at-once semantics; an unknown-drive cancel answered with an aborted frame) and the new-tab apply receiver (alias-first, the shared admission kernel, the ONE apply funnel with receiver-identical opts, spec by object identity, the always-sent terminal response), plus the static wiring / negative-postcondition locks of spec `[ap:7S217K]` §8 as amended by U01 `[ap:08ZEAO]` (five bridge kinds, ONE readiness notification site, no polling/sendToServer/tab-close on any drive path; the delay setting's one default + one clamp evaluated from the real hotkeys-shared.js).
- `test_project_maps_location_setting.js` — the extension's Project Maps location option: `DEFAULT_EXTENSION_SETTINGS.project_maps_location` is `''`; `mergeExtensionSettings` trims a string, stores it verbatim and falls back to `''` for anything else; the `get_extension_settings` reply carries `project_maps_location` and `default_markdown_folder` as a trimmed string or `null`; options.html places the field after the Default Note Sessions parent row (no Activate) and options.js binds, renders and persists it.
- `test_saved_tabs_drive_ui.js` — Saved Tabs (Tier-2, unit T2B; U01): the import-list multi-select + Go + progress console — the selection model (checkboxes keyed by row index, Select All/None, live-count Go dimmed at 0), Go-time validation (empty-selection hint; duplicate non-empty alias refusal naming each offender), the `saved_tabs_drive_request` sender (list order, spec by object identity; the ack's `superseded_drive_id` surfaced), the ONE progress branch painting per-entry dialog rows + the one status writer (the delaying state's detail included), Cancel-once, the loud refused-drive painting, the single-entry row-click apply regression — plus the U01 dialog-revert groups (after any summary/aborted frame the dialog is editable again with settled entries unticked and a one-click re-Go) and the header delay stepper (reads the merged mirror, writes through persistMdPreviewSetting, clamps through the real mergeExtensionSettings) — plus the static wiring / negative-postcondition locks of spec `[ap:7S217K]` §9 as amended by U01 (no sixth kind, no second status writer, no polling, no SW reach, no chrome.storage in the dialog).

## Run

```
node tests/js/test_dock_source_view.js
node tests/js/test_md_render_fixture.js
node tests/js/test_md_row_list_fixture.js
node tests/js/test_lens_modal_parity.js
node tests/js/test_carto_heartbeat_widget.js
node tests/js/test_saved_tabs_export_import.js
node tests/js/test_saved_tabs_tier1.js
node tests/js/test_f12_force_override_popup.js
node tests/js/test_slice_group_durable_read.js
node tests/js/test_project_maps_location_setting.js
node tests/js/test_saved_tabs_drive.js
node tests/js/test_saved_tabs_drive_ui.js
```

Run the three CARTO Jobs harnesses together through the receipt-backed wrapper from the repository root:

```sh
env -u WORKFLOWY_API_KEY -u WORKFLOWY_NEXUS_CONFIG -u WORKFLOWY_BROKER_TOKEN_FILE -u WORKFLOWY_MCP_BEARER -u WORKFLOWY_MCP_HTTP_DIAL_HOST python3 scripts/run_tests.py --files tests/js/test_carto_jobs_twenty_tabs.js tests/js/test_carto_jobs_details.js tests/js/test_carto_jobs_feedback.js
```

Exit code 0 = all pass, 1 = failure. **Zero dependencies** — no npm install, no
`package.json`, no framework. Plain Node (any reasonably recent version).

## Relationship to the Python suite

None, deliberately. pytest only collects `test_*.py`, so files here never
interfere with `tests/` Python runs, and this folder adds no step to the
day-to-day Python workflow. Run a JS harness manually when you touch the
client file it covers.

## The extraction idiom (how to add another harness)

The client (`glimpse-cache-client.js`) is one big main-world page script, not
a module, so it can't be `require`d. The harness instead:

1. reads the real client file bytes,
2. extracts each function under test **verbatim** (signature search + brace
   matching — see `extractFunction`),
3. evaluates it via `new Function`, injecting the handful of module-level
   locals the function reads as parameters,
4. asserts behavior against the exact shipped source.

A rename or signature change in the client fails **loudly** (extraction
throws), never silently. One file per feature area; keep harnesses
dependency-free.

Shared modules such as `glimpse-md-render.js` are loaded directly in a Node VM.
`fixtures/md-render/baseline.json` is immutable output captured from commit
`8dd6e0f` before extraction (provenance, source SHA-256 and capture command are
inside); it is not regenerated from the shared implementation during tests.

`fixtures/md-row-list/baseline.json` similarly captures the untouched `0c56c96`
row painter/helpers and emitted row CSS before their move. Its capture script
also supplies the dependency-free document shim and extraction utilities (no
copied production painter). See its README for fixed locale/timezone, immutable
hash, original replay and event/reference coverage. Row styles now live only
under `.glimpse-md-row-list` in the shared CSS; the original B1 host inventory
is still checked as retained panel rules plus these scope-normalized row rules.
