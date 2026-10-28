# Pre-extraction oracle

`baseline.json` was captured before production edits and committed alone in
`f95d7f0`. Its provenance records the actual command (`node tmp/capture-md-oracle.js`),
baseline commit, client SHA-256 and real marked/KaTeX/initializer hashes.

The one-shot capture script is archived as `capture-baseline.js`. It extracts
functions from the baseline client rather than storing another implementation;
it refuses a different client hash. To reproduce, use a throwaway checkout at
`8dd6e0f`, place this script at `tmp/capture-md-oracle.js`, then run the recorded
command from that checkout. Do not regenerate the oracle from new production
code. The fixture test reads only the immutable JSON, never this capture script.

For the extraction's mutation/replay evidence, run from the worktree root:
`node tests/js/fixtures/md-render/check-mutations.js`. It creates a throwaway
copy in `tmp/md-mutations`, kills six mutants (seven failing test invocations),
replays the capture against `git show 8dd6e0f` client bytes, and proves the new
API check fails before extraction. Production files are never mutated.
