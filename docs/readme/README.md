# README screenshots

These are the extension’s **real current UI on real NEXUS output**, captured
headless against a **stand-in Workflowy page**. The fixture loads the worktree’s
`workflowy-glimpse-cache/` CSS and page scripts unchanged. It does not connect to
Workflowy, the daemon, or a logged-in browser. No old bake-off PNG is reused.

The Pantry workspace is real: `https://workflowy.com/#/63a894ebbae14a0eb8ef6480cd8e0bc8`.
Its 36 nodes include three Anchor Docks in Product, Customers, and Team.
The Reader’s Markdown comes from actual tool renders, saved unchanged in
`example/`; the UUIDs, AP-IDs and tags belong to that workspace.

**Open limitation:** the daemon’s `slice_group.create` currently fails with a
cache-operation-boundary error. The two named picker definitions are therefore
fixture-only, not real saved groups. Offline mode and Sharing use real direct-tag
renders with the same full postfix policy. The requested saved-group verification
has **not** passed. See `example/provenance.json` for calls and scope evidence.
Jobs telemetry is invented for `pantry-app`; its Reader document is still real.

| Image | What it shows |
|---|---|
| `readme-hero.png` | Three separate branches and Docks, beside one Offline mode document. Start here. |
| `readme-docks.png` | The three-Dock picker with Descend on, beside a real Customer feedback render with `descend_policy: all`. |
| `readme-slices.png` | Pinned picker with two topic definitions, beside Offline mode. |
| `readme-help.png` | A real More… click from Anchor Docks into its Help section, with Back to Anchor Docks. |
| `readme-hero-dark.png` | The hero in the current dark theme. |
| `readme-tip.png` | First-run tip 3 of 3, including its closing line. |
| `readme-rod.png` | A real Go click for `basement OR signal`, with the actual search render. |
| `readme-jobs.png` | Modern synthetic running/completed job records and expanded Details. |
| `readme-palette.png` | Apply-slice-group palette filtered to `off`. |

## Re-shoot

From the repository root, on WSL with Windows Chrome installed:

```sh
bash docs/readme/capture.sh
# Or replace only selected stable files:
bash docs/readme/capture.sh readme-hero readme-hero-dark
```

The wrapper stages a fresh extension copy beside `h6/` under
`/mnt/c/tmp/nexus-readme-001/`, captures at 1500×950 CSS pixels and scale 2
(3000×1900 PNGs), and copies **only PNGs** back here. HTML dumps, Chrome stderr,
and throwaway profiles do not enter the repository. `NEXUS_CAPTURE_STAGE` and
`NEXUS_CAPTURE_OUT` override the scratch locations; keep both on `/mnt/c/` for
Windows Chrome. Example captures never load or stage `real-data.js`.

To refresh content rather than just re-shoot, repeat the calls in
`example/provenance.json`, save their Markdown with `output_file`, export only
the example root, and use `docs/ui-bakeoff/fixture/import-example.py` to import
that capture snapshot. Check the canonical row selections against fresh
`include_rows` replies before replacing the dataset.
