---
name: code-map
description: >-
  Start here before reading, explaining or changing this repository's source
  code. Its important functions, classes and regions carry `@beacon[...]`
  comment blocks that label them by topic. This skill explains those blocks and
  how to keep them current, points you to the slice label catalog
  (code-map/CATALOG.md: every label, what it covers and how many symbols carry
  it; read it first) and to the generated Markdown slices (code-map/slices/: one
  file per label, that topic's signatures and documentation across all files),
  and provides a Python script to list, read, tag, untag and regenerate them.
  Use it at the start of any coding task, whenever you add, change or remove
  code (the blocks, slices and catalog must stay true), whenever you meet an
  `@beacon[` block, and to set up the code map in a repository.
compatibility: Requires Python 3.10+ and git. Looking up JavaScript or TypeScript symbols also needs the tree-sitter parser; the script prints the install command when it is missing.
---

# Code map

This repository documents its code where the code lives. Important functions, classes and regions carry a structured comment block, a **beacon block**, directly above them:

```python
# @beacon[
#   id=billing@3,
#   slice_labels=billing,api,
#   kind=ast,
#   comment=POST /invoices: check, price and store one invoice,
# ]
def create_invoice(request):
    """The docstring, and comments right beside the block, travel with it."""
```

Each block gives its symbol one or more **labels** (`slice_labels`) and a one-line **comment**. Everything that carries a label, gathered from every file, is that label's **slice**: a compact Markdown map of one topic (file paths, kinds, signatures, labels, the block's comment, and the docstring or comments beside it), without the function bodies.

**Paths.** Files of this skill (`references/`, `scripts/`, `assets/`) are relative to this skill's folder, `.claude/skills/code-map/`. The code map itself, `code-map/`, sits at the **repository root** (`git rev-parse --show-toplevel` prints it). Run the script from the repository root:

```
python .claude/skills/code-map/scripts/code_map.py <command>
```

(Use `python3` where that is the interpreter's name.)

## 1. First: read the catalog

Read **`code-map/CATALOG.md`** before anything else. It lists every label in use, what it covers and how many symbols carry it. From it, pick the labels your task touches.

If `code-map/` does not exist, the code map has not been set up in this repository yet. Say so, and see *First-time setup* in [references/code-map-folder.md](references/code-map-folder.md) before creating anything.

## 2. Orient with slices, then read the code

- Read the slice for each label you picked: `code-map/slices/<label>.md`. Err a little on the generous side; slices are small.
- Then read the bodies you need, by name: `code_map.py read <name>` prints one function, class or method with line numbers, tagged or not. Long ones come back as head and tail, with the line range to read next. Ordinary file reads work too.
- A slice is a starting point, not a boundary. Follow calls by name into untagged code freely.
- `code_map.py slice <label>...` prints a fresh slice straight from the source when you suspect a committed file is out of date.

## 3. Keep the documentation true while you work

This way of documenting code was chosen for one reason: **documentation that sits beside the code it describes can be kept current by whoever changes the code, and that is usually you.** Treat the following as standing instructions for the whole task, not a one-time step:

- **When you change a tagged symbol, fix its block's `comment` and its docstring in the same edit** if what they say is no longer true.
- **New code earns a block only if it is an entry point**: something a person or agent would want to find by topic (a public handler, a substantive multi-step function, a class at the centre of a concern). **Curate, don't carpet-bomb.** A small helper that anyone reading its caller will see anyway gets no block of its own.
- **Reuse existing labels.** Choose from the catalog's descriptions, and mirror the labels of neighbouring symbols unless the new code clearly belongs to another concern. Add a new label only when nothing fits, and mention it in your summary.
- **When a symbol's concern changes, change its labels. When a block no longer earns its place, remove it.**
- **Never put code between a block and its symbol.** Comments may sit directly above or below the block.

The test for whether a symbol earns a block, the catalog duties and worked examples: [references/upkeep.md](references/upkeep.md).

## 4. Before you finish a change

If you added, changed or removed tagged code or labels, run these from the repository root:

```
python .claude/skills/code-map/scripts/code_map.py slice --write
python .claude/skills/code-map/scripts/code_map.py labels --write
python .claude/skills/code-map/scripts/code_map.py check
```

`slice --write` regenerates every slice file; `labels --write` updates the counts in the catalog and adds a row for any new label; `check` reports anything still wrong. Then write a description for every new catalog row (and correct any description whose label has drifted), and commit the regenerated files together with the code that changed them.

## 5. Commands

| Command | What it does |
|---|---|
| `labels` | Every label in use, with its count and its catalog description. `--write` updates the counts in `CATALOG.md` and adds rows for labels it does not list yet. |
| `slice LABEL...` | Prints the slice of one or more labels, from the source; a label selects its family (`demo` also takes `demo-cli`, never `demolition`). `--write` (no labels means all) regenerates `code-map/slices/` and removes files for labels no longer in use. `--headings` leaves out docstrings and comments. |
| `read NAME` | One function, class or method (`Class.method`) with line numbers. `--file PATH` when the name is ambiguous; `--start`/`--end` for a window; `--size` for the size only. |
| `tag NAME LABEL... [--comment TEXT]` | Adds labels to a symbol's block, creating the block if there is none. |
| `untag NAME LABEL...` | Removes labels from a symbol's block; removing the last one deletes the block. |
| `check` | Prints shared beacon findings as `path:line: severity code: message`, plus catalog and slice-file problems. Errors and warnings fail; info alone (including legal open-only spans and id resemblance) does not. Empty ids are not duplicates; repeated ids form one group row. |
| `init` | First-time setup: creates `code-map/` with a catalog template and fills in any labels already in use. |

Full reference: [references/commands.md](references/commands.md).

## More

- The block format in every supported language, span blocks, and exactly what a slice includes: [references/block-format.md](references/block-format.md)
- The `code-map/` folder, the catalog and slice-file formats, and first-time setup: [references/code-map-folder.md](references/code-map-folder.md)
- If this skill's text has dropped out of your context (long sessions get compacted), load the skill again.
