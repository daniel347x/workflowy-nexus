# `code_map.py`: command reference

Run from the repository root:

```
python .claude/skills/code-map/scripts/code_map.py <command> [options]
```

The script needs only Python and git. It reads the files git sees (tracked files, plus untracked files that are not ignored), keeps no cache, and parses only what a command needs, so it is fast on large repositories. Python symbols use the standard library. JavaScript and TypeScript symbols need the tree-sitter parser; when it is missing, the script prints the one install command. Beacon blocks themselves are read in every supported language without it.

Exit status: `0` success (including info-only `check` findings); `1` an error/warning or catalog/slice problem (`check`), a name or label that did not resolve, or a refusal stated below; `2` a usage error.

## `labels`

Lists every label in use, with the number of tagged symbols and regions carrying it and its description from `code-map/CATALOG.md` (or `(not in catalog)`), one line per label in label order:

```
demo	11	The demo family: constants, the store, its handlers.
```

Counts are exact: an entry carrying the label counts once towards it, whatever else it carries, and a label matches nothing but itself.

- `--write`: updates the **Symbols** count of every catalog row, and appends a row for every label in use that the catalog does not list to its `## New labels` section. Never deletes a row; never changes a description.

## `slice LABEL...`

Prints the slice of one or more labels, generated from the source: every tagged symbol and region carrying any of the labels, grouped by file, in the format described in [code-map-folder.md](code-map-folder.md).

A label selects its family: `x` also takes `x-…` and `x--…` (so `demo` takes `demo-cli`), but never a name that merely starts with the same letters (`demo` does not take `demolition`). The comparison is case-sensitive. A selection that matches nothing prints nothing and exits `1`.

- `--headings`: the compact form, without docstrings, comments or span source.
- `--write`: writes `code-map/slices/<label>.md` for each label given; with no labels, regenerates the file of every label in use and deletes files for labels no longer in use. Always the full form. Every label written must be a portable file name (no `<>:"/\|?*`, no control characters, and no two labels in use differing only in case); otherwise nothing is written.

## `read NAME`

Prints one function, class or method from the source, with file line numbers, whether or not it is tagged. `NAME` is a symbol name, `Class.method`, or a block `id`. A symbol's lines run from its first decorator (Python) to its last line.

A block `id` of a `kind=span` block reads the marked region: from the opening block's first line through the closing block's last line, blocks included. A `kind=ast` block's `id` reads the symbol it sits above.

- `--file PATH`: restrict to one file when the name occurs in several (the error lists the candidates).
- `--start N`, `--end N`: a window of file lines.
- `--size`: the file, the line range and the size only.

A symbol longer than 64 lines comes back as its first and last lines, with one row naming the omitted range; read it with `--start`/`--end`:

```
... | ... 14 lines not shown (52-65); read them with --start 52 --end 65
```

## `tag NAME LABEL... [--comment TEXT] [--replace-comment]`

Adds the labels to the symbol's beacon block. `NAME` is a symbol name, `Class.method`, or a `kind=ast` block's `id`; a span block's `id` is refused (spans are edited by hand).

Each label is lowercase letters, digits and hyphens (`demo`, `demo-cli`, `demo--x`); anything else is a usage error. When the symbol has no block, one is created directly above it (above its decorators), with `kind=ast` and the id `<first label>@<qualname>` — `-2`, `-3`, … are appended until the id is unused in the file. When the block exists, its labels come first and the new ones are appended through field-local edits. Unrequested fields, unknown assignments, an implicit kind and comment wrappers (including JSDoc) stay byte-identical.

The block's comment is kept. `--comment TEXT` fills it only when the block has none; a different existing comment is kept, and a warning naming both texts is printed (the labels are still added). `--replace-comment`, which needs `--comment`, overwrites it. When nothing would change, the file is not written and the command says `unchanged`.

- `--file PATH`: as for `read`.

## `untag NAME LABEL... [--file PATH]`

Removes the labels from the symbol's block. Only the labels named are removed, compared exactly and never by prefix (`untag NAME demo` leaves `demo-cli`). Removing the block's last label deletes the block, and nothing else. A symbol with no block is an error; none of the labels present says `unchanged`.

- `--file PATH`: as for `read`.

`tag` and `untag` verify the selected byte edit before publishing a binary,
fsynced temporary file with an atomic replacement. A repeated id elsewhere
does not prevent editing a uniquely attached symbol. An unresolved target
prints its candidates and exits `1` for that symbol; no prior `check` is needed.
The fingerprint comes from
the bytes used to read and map the source. A failed proof, a changed source or
an IO failure exits `1` with the reason and leaves that attempted edit unwritten;
it does not prohibit editing another symbol or file. No lock or retry is added.
If the attachment re-check fails, the original bytes are restored only while
the file still holds this invocation's own write. A newer save is left alone
and reported as a restore conflict. A save strictly between the final hash
check and replacement is still an instruction-gap race, not a serialization
guarantee.

## `check`

Prints shared beacon findings as `relative/path:line: severity code: message`:

- structural, field, comment, mapping and attachment findings, with codes such as
  `missing_id`, `block_not_mapped`, `writer_anchor_ambiguous` and `comment_rejected`;
- one `duplicate_id` group per repeated nonempty id, not one row per extra copy;
  two id-less blocks are separate `missing_id` findings, not a duplicate;
- legal open-only spans (`span_without_close`) and id resemblance
  (`id_unlike_symbol`) as info, never automatic edits or command failures.

It also keeps the kit's own checks:

- a label in use that the catalog does not list, a catalog row whose label is not in use, or a label listed in two rows;
- a catalog count that differs from the real count;
- a catalog row with an empty description;
- a slice file that differs from what `slice --write` would write, a missing slice file, or one for a label no longer in use;
- a file that cannot be read or mapped;
- JavaScript or TypeScript files holding blocks while the parser is missing.

Without `code-map/CATALOG.md`, `check` says to run `init` and exits `1`.
Errors, warnings and the kit-only problems above exit `1`; info alone exits `0`.
The language set is unchanged: Python, JS/TS, SQL, `.sh` shell and YAML; no
Markdown, Vue or `.bash` suffix. Writer-view authority names this kit's metadata
capability; it does not imply a fix command or expand `tag`/`untag` beyond
Python/JS/TS. All findings are read-only and do not require a check before edits.

## `init`

First-time setup. Creates `code-map/CATALOG.md` from `assets/CATALOG.template.md` and an empty `code-map/slices/`, then runs `labels --write` so that any labels already in use get catalog rows. Refuses to overwrite an existing `code-map/CATALOG.md`, and refuses when the JavaScript/TypeScript parser is needed (some file holds a block) but missing.
