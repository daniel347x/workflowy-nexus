# `glimpse-include-rows` — pre-S1 legacy recording

Immutable oracle for `tests/test_glimpse_include_rows.py`, S1 of the modal
row-list unit (contract [ap:OAPE16] §5.2 / §6.1 / §9.1).

## Provenance

| item | value |
|---|---|
| S1 base commit | `0c56c969dc9e58f6695f6d132780e6bc9fdee007` (tree `ad0ddb25…`) |
| extraction | `git archive 0c56c96 \| tar -x -C tmp/pre` (never a checkout) |
| `server.py` sha256 | `a225702518284d179e886847ebd82129a8bffc4f7172ef455aae8fbb89c182c0` |
| `scenario.py` sha256 | `8ae10abe8d25ac57d2d1250ae3c8f705d0661bfa837fc809830c913b3ee88ab2` |
| `legacy_outputs.json` sha256 | `70b045f09ea886caf3aab91360438e0a3fbebb5b0b5ea26f0d08c062bef5883f` |
| capture command | `GLIMPSE_CAPTURE_ROOT=tmp/pre <warm-venv>/bin/python tests/fixtures/glimpse-include-rows/capture_legacy.py --out …/legacy_outputs.json` |
| environment | Python 3.12.3, Linux/WSL ext4, no network |

The recording uses **pre-change bytes**, independently of concurrent production
edits, from the archived base, with `workflowy_mcp` imported out of `tmp/pre/src` (the
capture script refuses any other module location).

## Contents

* `results` — the full public `dispatch_operation` result for every scenario in
  `scenario.py` (`glimpse.run`, and `anchor.dock.render` for the Dock entry):
  Markdown strings, full/UUID dicts, a file summary, and the two error
  envelopes. `__TMP__` paths are normalized to `<TMP>`.
* `modal_authority` — for raw root, slice group, slice override, simple Dock,
  created-asc and modified-desc: the `rows` and `markdown` emitted by the REAL
  `_handle_render_slice_modal` Apply/push handler. This is the row authority
  (built by `_build_modal_rows(..., cache_by_id=...)` over the final forest),
  captured pre-change, including the repeated-ancestor occurrences and
  `created`/`modified` timestamps.
* `focused` — the pre-change focused re-render (tooltip → tap triple) of one
  real row, through the public `glimpse.run` render-spec path.

`edge_outputs.json` supplements (never regenerates) that oracle with a truly
empty curated window (fixed upstream pivot 1000; real selection/pruning), an
empty difference lens, node/empty-lens estates, and the actual pre-S1
`forbidden_parameter` error envelopes for both operations. Its recorder is
`edge_scenarios.py`; the script verifies the imported server SHA-256 above.
The original `raw_root_empty_window` scenario name is historical: its 1s
window is narrow but nonempty; the supplemental curated case exercises the
real empty-window return.

## Re-capturing

Do **not** regenerate this fixture to make a test pass. It is an oracle. If
the base commit ever changes legitimately, re-run the exact command above from
a `git archive` of that commit and update the hashes table.

## Mutation checks

`mutation_check.py` applies four reversible breakages in a throwaway tree:

1. drop the registry/impl `include_rows` threading;
2. drop `cache_by_id` from the Stage-2 `_build_modal_rows` call;
3. drop the `_resolved_render_spec` handoff;
4. recompose the envelope body without the finalized header/advisory string.

Each must turn its selected tests red. See the script's docstring for usage;
it refuses to mutate the live worktree.
