# Keeping the code map true

## Why this repository documents code this way

The documentation that matters most for working on code is the documentation of what each important piece is for. Here it lives **in the code, beside the symbol it describes**: a beacon block's `comment`, the docstring, the comments next to the block. That placement was chosen deliberately, to keep documentation current with the code:

- Whoever changes a function sees its documentation in the same edit, and agents in particular tend to keep adjacent comments true without being asked. This skill turns that tendency into a duty: **keep them true on purpose.**
- The slices are generated from the code, so they cannot drift from it as long as they are regenerated.
- The catalog says what each label means, so every agent and person starting work orients the same way, in minutes.

Three things are kept current, by you, as part of every change: **the blocks** (labels, comments, docstrings), **the slices** (regenerated), and **the catalog** (descriptions and counts).

## Does this symbol earn a block?

Two failure modes, and the second is as bad as the first:

- **Tagging nothing.** New code is invisible in the slices; the map slowly stops describing the code.
- **Tagging everything.** Every helper gets a block, every slice swells, and a slice that contains everything has stopped choosing anything.

**The test:** would someone reading the enclosing function, or the caller, already see this symbol, or find it at once by name? If yes, it needs no block of its own. If no, it earns one.

Blocks are for **entry points**: public handlers and endpoints, command-line entry points, substantive multi-step functions, the class at the centre of a concern, a critical region someone will need to find by topic. Not for thin predicates, one-line wrappers, private helpers and closures that serve one nearby caller. When in doubt, leave it out, and mention the choice in your summary.

## Labels

- **Reuse before you invent.** Read the catalog's descriptions and pick from them. A new function usually belongs to the same labels as its neighbours in the file.
- A symbol can carry several labels when it genuinely sits in several concerns (an endpoint can be both `billing` and `api`). Two or three is common; more usually means the labels are too fine.
- **A new label** is worth adding when a real concern has no home and several symbols will share it. Add it, run `labels --write`, write its description in the catalog, and say so in your summary so the person can veto it.
- **When a symbol's concern changes, change its labels.** When a label falls out of use, `check` reports it as listed but unused: remove its catalog row.
- Use `tag` and `untag` for label-only changes; they edit the block for you, create it when it is missing and delete it when its last label goes. When you are already editing the function, writing the block by hand in the same edit is just as good.

## Comments and docstrings

- **When behaviour changes, the `comment` and the docstring change in the same edit.** A stale comment is worse than none, because the slice repeats it to every reader.
- A good `comment` names what the symbol does and when someone would look for it: `POST /invoices: check, price and store one invoice`, not `invoice handler`.
- Renaming or moving a symbol: the block moves with it, directly above the definition.
- Deleting a symbol: delete its block too.

## The catalog: `code-map/CATALOG.md`

The catalog is the first thing every agent reads, so it has to be right. Its format is in [code-map-folder.md](code-map-folder.md).

- **Descriptions are yours to write and keep current.** One or two sentences per label: what it covers, where its main entry points are, anything a newcomer should know. Update a description when the label's meaning drifts.
- **Counts are the script's job.** `labels --write` sets the count beside every label and adds rows for new labels under *New labels*. Describe each new row and move it into the right section.
- Remove the row of a label that is no longer used, once `check` confirms it.

## Before you finish

After any change to tagged code or labels:

```
python .claude/skills/code-map/scripts/code_map.py slice --write
python .claude/skills/code-map/scripts/code_map.py labels --write
python .claude/skills/code-map/scripts/code_map.py check
```

Fix what `check` reports, describe new catalog rows, and commit the regenerated slices and the catalog together with the code. In your summary, mention any block you added or removed and any label you created.
