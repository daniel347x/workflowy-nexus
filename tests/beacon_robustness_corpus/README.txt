U01 G1 frozen witnesses -- a345dc780f0a6fed4b47a63b3fd13c914b15309f

These are quoted observations, NOT a second production parser and NOT approval
of the base's known defects. No test loads git, Workflowy or /mnt/c/tmp.
MANIFEST.json hashes every file (apart from itself). Preserve these old values;
name each later behavioral delta in ../beacon_robustness_expected_changes.json.

nexus_base/: 450 exact-byte source files from the base's git objects, stored as
.txt, plus parser records, diagnostics and the AST/callsite inventory. This
complete source set also supplies Area 10, avoiding duplicate 8.49 MB copies.
There are 1,607 block diagnostics and 1,604 kept records, not 1,607 kept blocks.
104 span-kind records includes the JS id-less pseudo-block. Area 10's 103 spans
have IDs. All five named adjacent-close pairing errors are pinned here.

area1/: uncapped C1+C2 stop-rule-only before/after output over the existing
54-source x 6-parser golden set. Twenty source/parser keys contain 575 changed
beacon records (549 surfaced, 26 anomalous; zero clean changes). Cross-language
keys: 17 JS, plus shell and YAML on the Python unterminated fixture. Quoted
measurement scripts record how the data was obtained; they are never imported.
The recon summary's claim that all 19 cross-language keys are JS is incorrect.

area1_q1/: the historical corruption inputs/results: 85 cases and six clean
bases, byte-exact. This is not the required final 15 x 6 = 90 after-state test
matrix. Add the missing cases and scanner assertions in the recovery tests.

area6/: 44 duplicate-hygiene inputs, 12 alias + 6 C0 + 8 EOF inputs, and recorded
outcomes. cur/sp/spg are recon variants, not the final U04 policy. In particular,
distinct-ID merges are observations of old behavior, not intended new merges.

area10/: all 1,603 windows, all 261 proposed corrections (258 non-test), 103
ID-carrying spans and the five parser-pairing mismatches. The manifest resolves
every window's source into nexus_base/sources. Later edits to real repository
files cannot change these witnesses. old_top/new_top are baseline/proposed
values, not a claim U07 has been implemented.

Paths into scratch inside provenance fields are historical descriptions only.
Sources retain BOM/newlines/EOF exactly; no live beacon blocks were added to
test-language files. Historical count discrepancies are explained above rather
than silently normalizing observations to a narrative count.
