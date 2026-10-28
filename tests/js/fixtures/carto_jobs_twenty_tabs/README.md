# `carto_jobs_twenty_tabs` — the daemon's recorded wire (WaveSpec §5.2)

`stream.json` is the RAW text the production daemon actually sent to the
websocket while the Python harness
`tests/test_carto_jobs_twenty_tabs_daemon.py` drove the twenty-tab witness. It
is the input of the page-side harness `tests/js/test_carto_jobs_twenty_tabs.js`
("before" and "after"), so both sides count the same bytes.

## Shape

```text
{"format": 1,
 "duration_seconds": 60,
 "scenarios": {
   "idle":    {"legacy": ARM, "v2": ARM},   # no job at all
   "running": {"legacy": ARM, "v2": ARM}}}  # one folder job, progress changes once a second

ARM = {"warmup": [raw text, ...],          # delivered before any counter starts
       "frames": [{"at": <seconds>, "text": <raw frame>}, ...],
       "scans":  [<seconds>, ...]}         # the daemon's off-loop scans
```

- **`legacy` is the "before" wire:** a poll with no `since`, answered with one
  full `carto_list_jobs_result` frame per scan.
- **`v2` is the "after" wire:** a poll carrying `since: <last generation>`,
  answered with a full `carto_list_jobs_result_v2` frame when the content
  changed, else with the tiny `unchanged` frame.
- **`at`** is the simulated second inside the measured window (`0.0` …
  `59.5`; four quarter-second ticks a second, five tabs polling on each tick,
  so twenty tabs each poll once a second). Bucket by `floor(at)` for a
  per-second count.
- **`warmup`** is the daemon's initial full frame at negative time. Feed it
  first and let the page remember it (its `generation` for `v2`), then reset
  the counters and process `frames`.
- Every frame is broadcast: every simulated tab receives every frame in the
  arm it is running.
- `scans` and `frames` are aligned one-to-one: one coalesced reply per admitted
  scan, whatever the number of pollers.

## Measured (printed by the Python test; regenerated with the fixture)

| arm | scans/s (max) | replies/s (max) | frames full/unchanged | bytes min | max | total |
|---|---|---|---|---|---|---|
| idle / legacy | 2 | 2 | 120 / 0 | 65 | 65 | 7,800 |
| idle / v2 | 2 | 2 | 0 / 120 | 102 | 102 | 12,240 |
| running / legacy | 2 | 2 | 120 / 0 | 696 | 697 | 83,622 |
| running / v2 | 2 | 2 | 60 / 60 | 102 | 663 | 45,891 |

## Regenerating

Never by hand. The test records this file only when asked, and otherwise fails
on any drift:

```bash
CARTO_JOBS_RECORD_STREAM=1 python3 scripts/run_tests.py --files \
    tests/test_carto_jobs_twenty_tabs_daemon.py
```

Under the NEXUS runner the write lands in the clone (`~/.cache/wnmt-tests/…`),
so copy that file back into this directory and re-run without the variable to
prove the committed bytes are the daemon's.
