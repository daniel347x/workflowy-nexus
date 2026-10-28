# The `code-map/` folder

## Layout

Two folders, kept apart on purpose:

```
<repository root>/
├── .claude/skills/code-map/     the skill: generic, the same in every repository
│   ├── SKILL.md
│   ├── references/
│   ├── assets/CATALOG.template.md
│   └── scripts/code_map.py
└── code-map/                    this repository's code map: specific to it, committed
    ├── CATALOG.md               the slice label catalog (read it first)
    └── slices/
        └── <label>.md           one generated slice per label in use
```

The skill folder holds nothing specific to this repository, so it can be upgraded by replacing it whole. Everything specific to this repository lives in `code-map/`, which an upgrade never touches.

## `CATALOG.md`: the slice label catalog

The catalog lists every label used in this repository: what it covers and how many symbols carry it. Every agent reads it first, so keep it accurate (see [upkeep.md](upkeep.md)).

**Format.** A short introduction, then one or more sections (`## Area name`), each holding one table:

```markdown
## Billing

| Label | Symbols | What it covers |
|---|---:|---|
| `billing` | 14 | Invoice creation, pricing and storage. Start at `create_invoice`. |
| `db` | 6 | Schema and queries for the invoice tables. |
```

- A row whose first cell is a label in backticks is a catalog row. Group the rows into sections however best explains the code (by area, by layer); a small repository can use one section.
- **Symbols** is the number of tagged symbols and regions carrying the label. It is maintained by `labels --write`; don't edit it by hand.
- **What it covers** is written and maintained by agents: one or two sentences, saying what the label covers and where to start.
- `labels --write` updates the **Symbols** cell of every row, and appends a row for each label it does not find to a final section, `## New labels`. It never deletes rows and never changes descriptions; `check` reports rows whose label is no longer used, and you remove them.
- Other text in the file (introductions, notes under a section) is left alone by the script.

## `slices/<label>.md`: the slice files

One file per label in use, named after the label. **Generated, never edited by hand:** to change a slice, change the code (blocks, comments, docstrings), then run `slice --write`.

Each file is plain Markdown:

````markdown
# `src/billing/invoices.py`

## function `create_invoice(request)`

Labels: billing, api

POST /invoices: check, price and store one invoice

```text
Check the request, price each line and store the invoice.
```

## class `PriceTable`

Labels: billing

...
````

- One `#` heading per file, as a path relative to the repository root; files sorted by path.
- One `##` heading per tagged symbol or region, in source order: its kind and its signature. Something tagged inside another tagged symbol (a method in a class, a region in a function) sits one heading level deeper, under it. A tagged symbol's enclosing classes and functions appear above it as headings even when they carry no label of their own; in the full form they print their own docstring.
- A `Labels:` line, then the block's `comment`, then the documentation found beside the block, in `text` fences: docstrings, JSDoc/TSDoc, comments directly above or below the block, and the source of spans marked `show_span=true`.
- No line numbers, dates or absolute paths, so a slice file changes only when a tagged symbol's signature, labels, comment or documentation changes, and its diff in a commit shows exactly that.

`slice LABEL --headings` prints the compact form (no documentation fences; `Labels:` and the comment are kept); the committed files are always the full form.

## When the files are regenerated

- **After every change to tagged code or labels, before committing:** `slice --write`, `labels --write`, `check` (see the skill's section 4). The regenerated files go into the same commit as the code that changed them.
- `slice --write` with no labels regenerates every file, writes files for new labels and deletes files for labels no longer in use.
- `check` reports any slice file that differs from what `slice --write` would write, so a stale file is caught even when someone forgot.

## First-time setup in a repository

1. Copy the skill folder into the repository at `.claude/skills/code-map/` (both Claude Code and OpenCode discover project skills there).
2. From the repository root, run `python .claude/skills/code-map/scripts/code_map.py init`. It creates `code-map/CATALOG.md` from the template and `code-map/slices/`, and, if the code already carries beacon blocks, adds a row for every label in use.
3. **Label the entry points.** Start small: a handful of labels that match how people talk about the code (its main areas, its layers), and blocks on the entry points of each. Use `tag`, or write the blocks by hand. The code does not need full coverage on day one; it needs the entry points.
4. Write a description for every catalog row and group the rows into sections.
5. Run `slice --write`, `labels --write`, `check`, and commit `.claude/skills/code-map/` and `code-map/` together.

From then on, every agent that loads the skill reads the catalog first and keeps the map current as it works.

## Upgrading the skill

Replace `.claude/skills/code-map/` with the new version and run `check`. `code-map/` is untouched. If the new version formats slices differently, `check` reports every slice file as stale; run `slice --write` and commit the result on its own.

## Harness notes

- Claude Code and OpenCode both find the skill in `.claude/skills/code-map/`. Each session sees the skill's name and description; the rest of `SKILL.md` loads when the skill is used, and these reference files only when they are read.
- In Claude Code, a personal skill of the same name (`~/.claude/skills/code-map/`) takes precedence over this one. In OpenCode, two skills with the same name conflict. Keep one copy.
