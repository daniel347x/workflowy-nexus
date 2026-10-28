# Agent kit

A standalone skill, **`code-map`**, for any repository whose code carries `@beacon[...]` comment blocks. It teaches agents what the blocks are, gives them a catalog of the repository's labels to read first and generated Markdown slices to orient by, and keeps them maintaining all of it as they change the code. It needs nothing but Python and git, and works in Claude Code and OpenCode.

## What is here

```
agent-kit/
├── README.md                          this file (not installed)
└── .claude/skills/code-map/           the skill, copied as is into a target repository
    ├── SKILL.md                       name + description (always listed) and the core instructions
    ├── references/
    │   ├── block-format.md            the comment-block format, per language; what a slice shows
    │   ├── upkeep.md                  why the method, and keeping blocks, slices and catalog true
    │   ├── code-map-folder.md         the code-map/ folder protocol, file formats, first-time setup
    │   └── commands.md                the script's command reference
    ├── assets/
    │   └── CATALOG.template.md        the catalog `init` writes into code-map/
    └── scripts/
        ├── code_map.py                the script (the part maintained by hand)
        └── _engine.py                 the generated engine (never edited by hand)
```

A target repository gets two folders: the skill at `.claude/skills/code-map/` (generic, replaced whole on upgrade) and its own `code-map/` (the catalog and the slices, specific to it and committed). See `references/code-map-folder.md`.

### About `_engine.py` (for maintainers)

`scripts/_engine.py` is **generated**: a build script kept with the upstream source regenerates it from that source, and nothing in it is edited by hand — a change wanted in the engine is made upstream and the engine is regenerated. Only `code_map.py` is maintained by hand here; it imports the engine and adds the command line on top.

## Installing

1. Copy `.claude/skills/code-map/` into the target repository at the same path.
2. From that repository's root, run `python .claude/skills/code-map/scripts/code_map.py init`.
3. Follow *First-time setup* in `references/code-map-folder.md`, or ask an agent to: the skill's description invites it.

## How the skill loads (why the text is split as it is)

| Layer | What | When the agent sees it |
|---|---|---|
| 1. Listing | `name` and `description` from the frontmatter of `SKILL.md` | Every session, from the start, whether or not the skill is used. Claude Code lists them in its skill listing (the description is cut at 1,536 characters there, and dropped first when many skills crowd the listing); OpenCode puts them in the system message, with the path of `SKILL.md`. This is the only text guaranteed to be seen, so it says when to use the skill and names the catalog. |
| 2. Body | the rest of `SKILL.md` | When the skill is used: the agent decides from the description, or the person types `/code-map`. It then stays in the conversation (Claude Code keeps its first 5,000 tokens through compaction), so it is kept short and the standing instructions come first. |
| 3. References | `references/*.md`, the catalog, the slices | Only when the agent reads them, because the body points to them. |
| Scripts | `scripts/code_map.py` | Run, never loaded. |

The frontmatter uses only fields of the open Agent Skills specification (`name`, `description`, `compatibility`), so it loads unchanged in both harnesses.

## Status

Complete. `scripts/_engine.py` is generated and committed, and `scripts/code_map.py` is hand-written on top of it: `labels`, `slice`, `read`, `tag`, `untag`, `check` and `init` all work as `references/commands.md` describes, and the skill's text matches the script as built.
