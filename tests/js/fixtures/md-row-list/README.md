# Immutable row-list oracle

Captured **before any S2 production edit**, from source commit
`0c56c969dc9e58f6695f6d132780e6bc9fdee007` (B1 extraction after rebase).

Command, from repository root:

```
node tests/js/fixtures/md-row-list/capture-baseline.js
```

Client SHA-256: `0c691876d9d5974f29328dec411a55339a61e8ee5fe6ea2320256c79695aa72f`.
Fixture SHA-256: `43bb24b85a184c5a0239939f96a2fa21121311b7d3033c08f8f0b1359da97767`.
Timezone `America/New_York`, locale `en-US` (`LANG=en_US.UTF-8`), Node's
Intl locale is asserted. Timestamps include the March daylight-saving boundary.

The capture extracts the **real** untouched painter/helpers by signature/brace
matching and executes them in a dependency-free Node VM. No painter body is
copied into a test implementation. The minimal document records ordered styles,
classes, attributes, text/inner content, child order and listener registrations;
each row is clicked with plain/Ctrl/Meta/Shift events, recording
stopPropagation → callback (index and identical event) → focus. `_rowEl`
references are checked against the actual appended row objects.

Ten cases cover depths 0/1/4/7 and outdents, long/blank/null/Unicode/raw-looking
labels, all icon kinds and halo combinations, full multi-selection, a primary
outside the set, no selection, empty input, both chronological axes/directions,
missing/invalid timestamps, old branch timestamps, same/multiple days and
nonmonotonic leaf blips. Canonical alias comparisons use these captured labels
as `name`; the no-icons case omits kind/collapsed entirely.

CSS is the real untouched style builder's emitted row rules, guides included.
It is compared after substituting **only** the container scope selector.
The capture refuses a changed client or an existing fixture. Never regenerate
`baseline.json` from the shared painter. To replay original bytes, use a
throwaway `git archive 0c56c96` tree with these test files and run:

```
node tests/js/test_md_row_list_fixture.js --legacy
```

Legacy comparisons intentionally pass there; the normal harness's new export,
delegate and single-owner checks fail before extraction. After extraction the
normal harness compares shared API, real delegate and canonical-name inputs
against the same immutable data. This proves offline fidelity, not physical
iPhone acceptance or the presence of live-DOM icons on canonical daemon rows.

Explicit sensitivity/replay command (not part of the normal fixture run):

```
node tests/js/fixtures/md-row-list/check-mutations.js
```

It changes only ignored throwaway copies under `tests/.runs/s2-row-mutations`, kills guide,
depth-spacing, selection-marker, tooltip, day-separator and delegate-state
mutants, and independently checks pre-move API/delegate/owner/class/CSS failures
alongside passing original-byte replay. Logs remain in that ignored directory.
