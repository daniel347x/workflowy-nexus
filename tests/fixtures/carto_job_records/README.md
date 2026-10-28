# Golden job-record fixtures (`summary-head/2` + evidence sidecars)

Fixtures for Unit JR2 (WaveSpec rev 3 §2.2-§2.6, §3.1 item 8).  Each fixture is
a `summary-head/2` record plus the append-only evidence sidecars the layout
puts next to it (`files.jsonl`, and `nodes.jsonl` for a bulk job), produced from
a frozen *source* record in today's layout by [`build_fixtures.py`](build_fixtures.py).

The original `record.json`/`files.jsonl` stay G2 reader fixtures, and
`writer.record.json` keeps the independently built G3 counterpart (Q4,
[ap:P0YV8T]). G4 adds `marker.files.jsonl` and `marker.writer.record.json`:
the current writer reproduces those evidence bytes and record values exactly,
timestamps excepted. D3-1 replaces only the top-level/projected planned outline
with its null or count/digest marker; finding anatomy remains complete. Digests
use compact sorted-key default-ASCII JSON. The builder never imports or calls
the writer. `--check` verifies every oracle; original fixture bytes stay fixed.

The counterparts add B5's exact warning total/display, count every landed row
section (Markdown's hint is 2), and keep count-bounded previews inline. The
synthetic interrupted-bulk original remains reader-only: append → record order
publishes receipts before a normal boundary, so its stale crash head is not a
finalized writer oracle; its counterpart counts the landed rows/receipts while
keeping the placeholder. No original source, record or evidence bytes change.

## Layout

```
tests/fixtures/carto_job_records/
  build_fixtures.py                 the committed builder (deterministic; --check)
  capture_joined_fixtures.py        capture plugin for the two harness fixtures (see below)
  build_g5_baseline.py              freezes the fc79975 bulk runner definitions (dev-time; --check)
  g5_baseline.json                  those frozen definitions: the A1 oracle arm's source
  folder_refresh_trim/
    sources/folder_refresh_trim.record.json          trimmed from the 1,063-row record
    sources/folder_refresh_trim.legacy.record.json   the same payload, legacy key order
    record.json  files.jsonl                         (/2 counterpart; the legacy twin reuses it)
  mixed_folder/
    sources/mixed_folder.record.json                 synthetic, written by the builder
    record.json  files.jsonl
  bulk_receipts_synthetic/
    sources/bulk_receipts_synthetic.capture.json     synthetic write sequence
    record.json  files.jsonl  nodes.jsonl
  bulk_apply/          sources/bulk_apply.capture.json   MISSING: one J-3 harness sequence
  markdown_generate/   sources/markdown_generate.record.json  MISSING: one J-4 final record
```

**One source convention:** every frozen input lives in `<fixture>/sources/`
(installer and differential comparisons both read it there).

The legacy-order twin has **no duplicate `/2` pair**: the build proves its
counterpart is byte-identical to `folder_refresh_trim/` (same rows, same lines),
so tests reuse those files and the tree stays 945 KB lighter.

A fixture directory mirrors one job folder: `record.json` carries
`evidence.files.path = "files.jsonl"` (and `evidence.nodes.path = "nodes.jsonl"`
for a bulk job), so a reader may open it exactly as it opens a live job folder.

## Rebuild and verify

```bash
# rebuild every fixture from its committed source
python3 tests/fixtures/carto_job_records/build_fixtures.py

# verify the committed bytes (exit 1 on stale; missing sources are reported)
python3 tests/fixtures/carto_job_records/build_fixtures.py --check

# verify the frozen fc79975 definitions the A1 oracle arm runs (needs .git)
python3 tests/fixtures/carto_job_records/build_g5_baseline.py --check

# re-create the synthetic mixed-folder source
python3 tests/fixtures/carto_job_records/build_fixtures.py --write-mixed-source

# re-trim the folder source from the archived copy (never edits the copy)
python3 tests/fixtures/carto_job_records/build_fixtures.py \
    --trim-source /mnt/c/tmp/carto-jobfile-eval/snap1/20261026-114908_carto-refresh-folder-844471b0.json
```

The builder needs the repository virtualenv (`pydantic` etc.) because it
recomposes a `summary-head/1` source with the production batch composer --
`.venv-wsl/bin/python` here.  No timestamps are generated, so two runs produce
identical bytes.

## Provenance

**`folder_refresh_trim.record.json`** is trimmed from the archived folder
refresh `/mnt/c/tmp/carto-jobfile-eval/snap1/20261026-114908_carto-refresh-folder-844471b0.json`
(59,555,434 bytes, sha256 `db0cd6ceb9f1e4f1d269388a364ae3ca2207d0295972ac7fada06ecc59cd9acc`,
`summary-head/1`, 1,063 rows, `status: running`, a mid-run snapshot of
`cartographer_jobs`).  The copy is never edited and the live
`cartographer_jobs` tree is never touched.

What the trim changes, and only this:

* **rows** -- a deterministic selection of the real rows: the record's 4
  ignore-skip rows (all), every changed row (all 18), three clean rows, the
  smallest row carrying an error-severity finding, and then the smallest
  finding-bearing rows that still fit the 2 MB cap (5 of them; 30 rows in
  total, 9 of them with findings, the largest row 398,694 bytes).  Row content
  is untouched, `reconcile_children` included (D3-1 is G4's, not applied).
* **`result_summary`** -- recomposed with the production
  `aggregate_beacon_outcomes` + `beacon_job_outcome` over exactly those rows,
  i.e. the record today's `save_summary` writes for them, plus the folder
  loop's counters (`files_refreshed`, `new_files_created`, empty `errors` /
  `refresh_errors`).  Never a hand-edited total.
* **`progress.total_files` / `completed_files`** -- the kept row count, so the
  counts agree with the rows.  The other head fields, `status: running`
  included, are the captured record's.
* The builder writes the file in the runtime's own `/1` layout (head indented,
  `result_summary` tail compact), so the head-only reader path is exercised on
  a realistic file.

**`folder_refresh_trim.legacy.record.json`** is the identical payload in legacy
key order: the layout marker removed and `result_summary` placed after
`progress`, so no reader can mistake it for a head-first record.  Every value is
unchanged.  Its `/2` counterpart is byte-identical to the `/1` twin's, which is
the point (key order cannot change the detail).

**`mixed_folder.record.json`** is synthetic (written by the builder, deterministic):
one small folder run with a clean file, a corrupt file (`mapper_parse_error`,
existing nodes preserved), an unreadable file (`not_utf8`), a new file, a failed
file (`io_error`), an ignored file (`skipped_by_ignore`), a warning-only file,
and one file with 250 findings (so the 200-finding preview truncates).  It
carries two run-level errors (so `item errors` / `item refresh_errors` /
`item run_events`) and `present_paths` (so `item present_paths`).  It is the
only terminal-outcome fixture and the only one with run-level items.

**`bulk_receipts_synthetic.capture.json`** / `bulk_receipts_synthetic/` is
**synthetic, and labelled so in its own source** (not harness provenance).  It is
the `/2` evidence for WaveSpec §3.1's *Bulk receipts* cases, built from a
synthetic write sequence so the fold's delta overlay and receipt rule can be
exercised before the harness fixture exists:

| file | case |
|---|---|
| `own_then_other.py` | A1(iii): own group refreshes, then another group writes a node; the refresh keys (`status: refreshed`, `refresh_outcome`, `file_node_id`, counters) survive beside both receipts |
| `own_then_other_rejected.py` | (ii) own group first, the other group's receipt rejected → `status: failed`, `nodes_satisfied: 1` |
| `other_then_own_rejected.py` | (ii) other group first (its `findings` are kept), then the own group's receipt is rejected |
| `interrupted.py` | (i) abrupt end: receipts in `nodes.jsonl`, **no row line**, the record names it in `in_flight` (the fold must add the interrupted row) |
| `in_sync.py` | (v) every candidate passed the in-sync filter → `node_outcomes: []`, `nodes_satisfied: 0` |

A1(iv) (a cancellation inside a group) is **deliberately absent**: it depends on
the harness case and on the G5 writer's stop-path choice, which this fixture
must not invent.

**`bulk_apply.capture.json`**, **`markdown_generate.record.json`** and the
**`a1_traces/sources/`** captures are frozen from the J-3 / J-4 joined witnesses and the
three A1 witnesses at the G2 base (WaveSpec §3.1 item 8).  No real bulk or
Markdown record survives on disk (history was cleared on 2026-10-26), so a bulk
record's delta lines can only come from that harness's *write sequence*.
[`capture_joined_fixtures.py`](capture_joined_fixtures.py) documents and
implements the capture mechanism: post-write on-disk records **and** the real
aggregate INPUT rows (`aggregate_inputs`, observed non-mutating), each document
naming its test, commit and writer provenance (the two funnel definitions Q1
moved into G2 are rebound to their base source for the capture run and the
substitution is recorded).  The builder ingests both shapes -- `{"kind",
"captured_from", "records": [...], "aggregate_inputs": [...]}` for a captured
sequence, a plain record for Markdown -- and refuses to invent deltas without a
sequence.  The three A1 witnesses: (iii) B's own group refreshes B and a later
frozen-grouping group finalizes it again with the same stable node id (a
different #tag), keeping `status: refreshed`, `refresh_outcome`, `file_node_id`
and the refresh counters beside both receipts; (iv) another group writes into B,
then B's own group is cancelled inside its writer; (v) the canonical mapped
name+note makes every candidate pass `_node_name_tags_match_beacon_block_in_note`
(the server's in-sync filter, before any writer call), giving a row with
`node_outcomes: []` and `nodes_satisfied: 0` and no receipt at all.

## Invariants the builder enforces

* §2.4 line grammar: one compact JSON object per line, `\n`-terminated, and
  **`ensure_ascii=True`** for every evidence line (contract B1 / CF-167, which
  overrides §2.4 rule 1).
* §2.6 single store: `projection` is `null` at its original key position with
  `proj` beside `row` when, and only when, **every projected key's value is deep
  JSON equal** to the row's own (§2.6's criterion -- dict key order never
  decides).  An **empty** projection mapping stores `null` + `proj: []` too.
  Where deep equality holds but a projected *value's* own nested key order
  would not survive re-expansion, the build **reports** it
  (`PROJECTION ORDER NOTES`) and still single-stores -- §2.6's criterion is deep
  equality, "per-row key order" is the row's field order, and `proj` preserves
  the projection's own key order.  The archived record has no such row.  `expand_row()` reproduces every stored row byte for byte, and the
  builder asserts that round trip on each one.
* §2.4 rows are the row **as the job gives it to the aggregate**: a bulk row's
  aggregate-generated `check_target` default is dropped, so the stitched row's
  key order equals today's row (the aggregate re-appends it last).  A
  `check_target` the JOB set survives, even if `final_observation` later flips
  false — proven by the captured aggregate INPUT rows when the capture has them
  (`aggregate_inputs`), else by `_job_row`'s evidence (a `refresh_outcome`
  carrying it, a position that is not the aggregate's appended-last, an earlier
  proof for the same file), and a derivation is reported (`JOB ROW PROVENANCE`).
* §2.4/CF-159 run-level lists: `nexusignore_sources` and `missing_paths` keep a
  **bounded preview** in the record (`CF159_PREVIEW_LIMIT = 20`) and their
  **complete** list in the evidence file; a key the result does not carry gets no
  preview at all.  `present_paths` is NOT previewed and never enters the
  structured head (§2.2): a non-empty list is `item` lines, an **empty** one is a
  `set` line with the tuple path `["result_summary", "present_paths"]` and `[]`,
  and an absent one stays absent.
* §2.4 run-level lists: `errors`, `refresh_errors`, `present_paths` **and any
  other list the folder result carries** (`nexusignore_sources`,
  `missing_paths`) become `item` lines when non-empty, so the record holds no
  complete list (CF-159/B8).  An **empty unknown** list has no item to carry its
  presence and no head preview, so it gets the same `set`-line treatment.
* §2.2 moved values use **unambiguous tuple paths**: `["result_summary", name]`
  for a summary value, `[name]` for another top-level field (the reader's
  `_restore_carto_value` restores tuple paths directly).
* §2.2 record: `record_layout` first; the head (with `outcome` and the three
  facets, §2.3); every other top-level field in source order; `evidence`;
  `in_flight`; `result_summary` last.  The summary carries every non-list value,
  the new aggregates (`errors_total`, `node_errors_total`, `phase8_failed`,
  `files_changed_total`, `events_total`, `findings_by_severity`, `phase8_summary`
  when the job has a phase 8), the four previews with `preview_totals`, and no
  tail list.  A value over 4 KiB moves to a `set` line with a
  `{"$evidence": "files.jsonl", "bytes": n}` marker in its place.
* §2.5/§4 item 1 receipt rule and finalizations: a bulk row stores
  `node_outcomes: null` with `"nodes": N`; every receipt is a `node` line naming
  its physical file key, appended as its batch returns.  In a captured sequence
  a row line is appended **only at a real group finalization**
  (`progress.completed_files` grew and the group-end write already carries
  `final_observation: true`) and at the terminal/stop snapshot — never for a
  group-head placeholder or a per-candidate progress write.  Order at a normal
  boundary: the active file first, then the other files the group touched, in
  today's `files` order; at a stop, that order as it stands.  A file that already
  has a row line gets a **DELTA**, never a rebuilt row; a delta line is emitted
  even when only its reflected receipt count changed.  The builder refuses any
  sequence whose receipt list is not an append-only prefix of the final one.
* Previews are honest about key presence: `files_changed` always, `errors` /
  `refresh_errors` only when the producing result carries them (a bulk result
  has `errors` and no `refresh_errors`, so no phantom key appears in a stitched
  bulk detail); `findings_preview` is new and always present.  Severity counts
  and the findings preview come from the **composed** findings list (a phase-8
  row replaces its file's findings, never adds to them).
* `derive_outcome` implements CF-174's legacy fallback
  (`warnings_by_type.phase8_refresh_failed` counts as a phase-8 failure, so a
  head read of a `/1` Markdown record is not labelled `completed_with_warnings`).
* Chronology: a **captured sequence** produces lines in the run's own per-batch
  order (receipts, then the batch's rows/deltas, then its run-level items) and
  `sidecar["sequence"]` records the call events it stands for (kind, file,
  `group-final`/`stop`, reflected receipt count) — that is the metadata G3's
  byte oracle compares, so a writer's own line order can be checked without
  pretending the base capture was JSONL.  The BASE runtime emitted a whole JSON
  record at every persist, not JSONL lines: this sidecar is a **normalized
  reconstruction**, and the README, the builder docstring and each capture
  document say so.  A fixture built from a *final* record cannot know the write
  order, so its line order is a canonical reconstruction and is not a byte
  oracle for a live writer.
* Determinism: `--check` rebuilds in memory and compares bytes.

## Measurements (the archived record, WSL, `.venv-wsl`)

All 1,059 rows that carry `projection` satisfy both halves of §2.6's equality
(deep equality and identical canonical encoding), key order included; none
fails.  Compact evidence-line bytes for those 1,059 rows fall from 31,681,482
to 17,125,398 (-45.95%) with the single store, `proj` list included.

## Builder API (proposed; implemented as written, pending the lead's agreement)

The builder is also importable, so a test can build a fixture into a tmp dir
instead of reading the committed bytes:

```python
import build_fixtures as bf            # tests/fixtures/carto_job_records on sys.path

bf.compact(value)                      # the ONE evidence-line encoder (§2.4 + B1)
bf.store_row(row, *, null_keys=())     # -> (stored_row, proj_keys | None)      §2.6
bf.expand_row(stored_row, proj_keys)   # -> the original row, byte for byte   §2.6
bf.same_json(left, right)              # deep JSON equality (JSON kinds tagged)

bf.derive_outcome(record)              # §2.3's outcome + three facets
bf.new_aggregates(summary, rows, *, node_receipts=(), phase8=None)
bf.previews(summary, rows)             # the four previews + preview_totals
bf.moved_values(source)                # §2.2's 4 KiB rule (tuple paths)
bf.presence_sets(summary)             # empty-list presence `set` paths, §2.2/§2.4

bf.build_sidecar(source, *, bulk=False, moved=None)
bf.build_sidecar_from_sequence(records, *, kind="bulk", aggregate_inputs=())
                                       # one row per group finalization; deltas after
bf.build_record(source, sidecar, *, files_name="files.jsonl",
                nodes_name="nodes.jsonl", moved=None, in_flight=None)
bf.dump_record(record)                 # indent=2, no trailing newline (writer parity)
bf.legacy_order(record)                # the legacy key-order variant (§2.11)
bf.roundtrip_row_check(source, sidecar, bulk=False)   # §2.6 assert
```

`build_sidecar_from_sequence` returns, beside `files_lines`/`nodes_lines`/
`counts`: `sequence` (the ordered call events this reconstruction stands for:
`{"k": "node"|"row"|"delta"|"item"|"set", "file", "cause":
"receipt"|"group-final"|"stop"|"presence"|"moved-value", "active", "receipts"}`),
`moved`, `presence_sets`, `in_flight` (for a non-terminal sequence),
`derived_job_rows` (records whose job rows were derived, not observed) and
`reinserted_keys` (a finalization that moved a key, recorded as `unset` + set).

Fixture entry points: `bf.fixture_targets()`, `bf.build_one(name, path, bulk)`,
`bf.trim_source(archived_copy)`, `bf.mixed_folder_source()`,
`bf.synthetic_bulk_source()`, and `main(argv)`.

Two placements are the lead's to settle (they are constants here, so a change is
one edit): the ordering of the new aggregate/preview keys inside
`result_summary` (`new_aggregates` then `previews`), and whether a Markdown
recording also emits `set publication` when the value fits inline in the record
(this builder always emits the `set` line, per §2.13).

## Checked against the production read side (G2's `carto_job_record.py`)

While this builder was being written, G2's read side landed in the working tree.
Every `.jsonl`/record pair here was folded with `fold_carto_job_evidence` and
stitched with `stitch_carto_job_detail` (read-only, scratch script, no test):

* `folder_refresh_trim`, `folder_refresh_trim_legacy`, `mixed_folder`: the
  stitched `result_summary` is deep-equal to the source fixture's, **including
  per-row key order**, with the same row count, no missing/extra summary key and
  the same top-level values outside §2.10's additive keys; the record's stored
  `outcome`/facets equal `carto_job_outcome(record)`.
* `bulk_receipts_synthetic`: the fold produces the five expected rows (the
  interrupted one from `in_flight`), the receipt attachments and tallies, the
  delta overlays, and the A1(iii) refresh keys.  It also exposes one seam for
  the reader: `stitch_carto_job_detail` leaves the record's `refresh_errors: []`
  preview in the stitched detail for a kind whose today's detail has no such key
  (bulk) -- the folder fixtures do not show this because their source summary
  carries `refresh_errors`.
* The stitched summary's *list* keys (`errors`, `warnings`, `events`,
  `findings`, `files`, ...) land at the end of `result_summary` instead of their
  source positions.  Deep equality is unaffected; a byte-order assertion on the
  summary would see it.

## The A1 witnesses and the frozen runner oracle: `tests/test_carto_bulk_golden_jr2.py`

The three A1 cases WaveSpec §3.1 asks for are real joined witnesses run TWICE on
the same scenario, each arm in its own `real_job_environment` (its own client,
graph, jobs directory and registry) at ONE environment path: the frozen arm runs
first and its tree is removed before the live arm builds pristine bytes at the
same path. A1(iii) a file's own group refreshes it and a later frozen-grouping
group finalizes the same physical file with the same stable node id (its refresh
keys and both receipts survive), A1(iv) another group writes into the file first
and the file's own group is then cancelled inside its writer, A1(v) every
candidate passes the server's in-sync **filter**
(`_node_name_tags_match_beacon_block_in_note`, before the writer) so the file has
`node_outcomes: []` and `nodes_satisfied: 0`.

Each arm's rows are read through the real `system.job.status` `view="detail"`
view joined across its pages, never the record folded against itself. The oracle
arm rebinds **only** the frozen server bulk runner/start/writers and the
governance admission (`g5_baseline.json`, 3 modules / 14 definitions; the bulk
family) and nothing else: the client writer, the record funnel and the reader are
today's on both sides, so the arms differ in the runner that produces the
evidence. The differential is the row comparison -- values, per-row key order,
`node_outcomes` order and `None` vs `[]` included -- after each arm's own random
job id and the clock stamps/pid in its artifact directory names are canonicalised.
Both arms also draw their auto-beacon suffixes from one fixed sequence, because
that suffix lands in the beacon block and therefore in the file's `file_sha256`
and every `finding_id`; and they run at one path for the same reason -- a
`finding_id` hashes the canonical file. D3-1's documented outline marker is
applied to BOTH arms through `build_fixtures.py`'s own independent `marker_row`,
so the transform itself is never a differential while its count/digest still
proves the outline. A1 (iii)-(v) reject no node, so nothing is weakened for the
CF94 rejection witnesses. The oracle arm also asserts each installed function's
compiled origin is `fc79975:<module>.<name>`, which is what keeps this a real
differential rather than today's code compared with itself.

`g5_baseline.json` is generated by `build_g5_baseline.py` from `git show
fc799756:...` alone -- it never imports today's writer, product code or this
repository's builder -- and carries each module's sha256 plus every definition's
own sha256, which the test re-verifies before compiling it. It is a
development-time generator: the test runner's ext4 mirror has no `.git`, so the
test only reads the committed JSON. Re-freezing is deliberate, not routine.

`test_frozen_*` need no daemon and prove the committed fixtures (the synthetic
bulk case, J-3, and the three A1 traces) against the production reader -- one of
them asserts a later finalization is a DELTA and never a rebuilt row, and
`test_builder_*` pins the builder's own grammar edges (empty `proj: []`,
`{}`/`[]`/`true`/`1` deep equality, empty presence sets, count-only deltas, a
job-set `check_target`).  A source not captured yet is a SKIP naming the file.

## What this directory does not do

* It does not import, call or shadow the production writer; G3 uses these bytes
  as its oracle.
* It does not run tests, edit the live job records or the archived copy.
* It does not claim harness provenance for a fixture that no harness produced.
