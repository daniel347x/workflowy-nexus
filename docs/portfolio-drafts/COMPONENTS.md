<!--
Draft from the portfolio program (stage D, the components file). Its final home is the root of
workflowy-nexus as COMPONENTS.md, so every path below is written relative to that root. Before
publishing, resolve every [TODO Dan: …] and [CONFIRM Dan: …] and replace or delete every 📸 stub.
-->

# NEXUS: the components

This page lists every piece of NEXUS: what each one is, how far along it is, and which repository and README to read for more. It describes what exists; it is not a roadmap.

> [TODO Dan: confirm the repository names. This page uses the plan's proposed names: `workflowy-nexus` (this repository) and `nexus-harness` (the Manager App and the harness). Replace them throughout if you choose others.]

## The shape of it

> *"NEXUS is a Workflowy-native context-management system for agents and humans working together. Workflowy is the single shared canvas — projects, decisions, communications, notes — and source code is mapped live onto it from disk by Cartographer. The agent reads and writes the same tree the human sees: no copies, no exports, no drift."*
> — the NEXUS server's own description, which every connected agent reads

The system has two halves, and then the same two halves again.

- **The tooling** ([`workflowy-nexus`](https://github.com/daniel347x/workflowy-nexus), this repository) is half knowledge work and half source code. The knowledge-work half is Anchor Docks and Anchor Points, tags and slice groups, GLIMPSE and the Markdown modal. The source-code half is Cartographer's maps, beacons, beacon slices and live snippets.
- **The agentic harness built on the tooling** has the same two halves. **Manager Sessions** is the knowledge work: an agent that helps you think, plan, remember and curate. **The Program Conductor Toolkit** is the source code: agentic swarms that implement, audit and remediate. Both are written as Workflowy subtrees. Both run on the harness software in [`nexus-harness`](https://github.com/daniel347x/nexus-harness): the Manager App, a host for Claude Code, the Manager's command line, and a layer over OpenCode.

All of it is Workflowy-centric, and it is all of a piece.

```text
Workflowy            one shared outline: notes, plans, agent memory, mapped code
    ▲
    │  MCP · 8 tools · 74 verbs
workflowy-nexus      NEXUS server · Cartographer and beacons · GLIMPSE Chrome extension
    ▲
    │  read and followed through NEXUS
Workflowy subtrees   Manager Sessions (knowledge work) · Program Conductor Toolkit (swarms)
    ▲
    │  run by
nexus-harness        Manager App · Claude Code host · Manager CLI and seats · OpenCode layer

(Beside all of this: the Agent Kit, which brings beacon slices to any repository without Workflowy.)
```

## At a glance

| Component | What it is | Status | Lives in | Read more |
|---|---|---|---|---|
| **NEXUS MCP server** | Gives any MCP-capable agent a Workflowy account as structured, shared memory: 8 tools, 74 verbs | Working, used daily | `workflowy-nexus` · `src/workflowy_mcp/` | [NEXUS README](README.md) |
| **The tool descriptions** | The tools teach themselves: an always-on description per family, then `describe` on demand | Working | `workflowy-nexus` · inside the server | [NEXUS README](README.md) |
| **Cartographer and beacons** | Maps a repository into Workflowy, one node per folder, file, class and function; beacon comments on disk become tags | Working, used daily | `workflowy-nexus` · root modules and the `beacon` tool | [NEXUS README](README.md) |
| **GLIMPSE Chrome extension** | The human side on workflowy.com: the docked Markdown modal, live-page reads for agents, hotkeys, Cartographer controls | Working, used daily · load unpacked | `workflowy-nexus` · `workflowy-glimpse-cache/` | [extension README](workflowy-glimpse-cache/README.md) |
| **Agent Kit** (`code-map` skill) | Beacon slices and live code reads from disk alone: no server, no Workflowy | En route | `workflowy-nexus` · `agent-kit/` | [Agent Kit README](agent-kit/README.md) |
| **Skill file and agent guide** | A five-line skill that loads one public guide | In development | `workflowy-nexus` | [NEXUS README](README.md) |
| **A packaged Workflowy agent** | The skill and the server, bundled as one install | Planned | — | — |
| **Manager Sessions** | The instructions that make an agent the Manager: thinking partner, memory curator, operator of programs | Working, used daily · not yet shared publicly | a Workflowy subtree | [Harness README](https://github.com/daniel347x/nexus-harness#readme) · [NEXUS README](README.md) |
| **Program Conductor Toolkit** | The doctrine for agentic swarms: a Conductor dispatches workers through Workflowy, audits their work and remediates it | Working, used daily · not yet shared publicly | a Workflowy subtree | [Harness README](https://github.com/daniel347x/nexus-harness#readme) · [NEXUS README](README.md) |
| **Manager App** | The desktop and phone cockpit: dictate, refine and send to the Manager; watch every program; read Workflowy views | Working, used daily | `nexus-harness` · `transcription-pane/` | [Harness README](https://github.com/daniel347x/nexus-harness#readme) |
| **Claude Code host** | Starts and supervises official Claude Code sessions: the Manager, Conductors and lead seats | Working, in daily use · young | `nexus-harness` · `scripts/claude_headless/` | [host documentation](https://github.com/daniel347x/nexus-harness/blob/main/docs/claude-headless-host.md) |
| **Manager CLI and seats** (`oc_*`) | The Manager's command line: the program view, steering, quick programs, warm forked seats, telemetry | Working, used daily | `nexus-harness` · `scripts/` | [Harness README](https://github.com/daniel347x/nexus-harness#readme) |
| **OpenCode layer** | A payload proxy, five plugins, the master config, and safe restart and upgrade | Working, used daily | `nexus-harness` · `proxy/`, `plugins/`, `config/` | [Harness README](https://github.com/daniel347x/nexus-harness#readme) |
| **Agents files** | The global worker instructions, the multi-model cast, the Manager and Conductor prompts | Working · written for this setup | `nexus-harness` · `agents/` | [Harness README](https://github.com/daniel347x/nexus-harness#readme) |
| **CodeNomad extension** | Connects a third-party OpenCode web client to the Manager App | Working, used less | `nexus-harness` · `chrome-extension/codenomad-session-watcher/` | [its README](https://github.com/daniel347x/nexus-harness/blob/main/chrome-extension/codenomad-session-watcher/README.md) |

**The evidence, in numbers** (measured October 2026):

- **NEXUS:** about 168,000 lines of first-party code, 5,402 test functions, and about 1,300 commits since January 2026, with the history public.
- **The harness:** about 147,000 tracked lines, about 1,800 Python test functions and 38 JavaScript test files.

### What the status words mean

- **Working, used daily:** runs every day in its author's own work.
- **Working, in daily use, young:** in daily use, but built in October 2026.
- **En route:** built and tested, but nobody outside this project has used it yet.
- **In development:** being written; not yet published.
- **Planned:** designed, and some of its pieces exist, but it is not packaged.
- **Used less:** still working, but its author's daily routine has moved past it.
- **Not yet shared publicly:** it exists and runs, but lives in its author's private Workflowy. It is described here, and shown in screenshots, until a curated public copy exists.

**Built for one person's daily work.** Everything here grew out of one developer's working setup: Windows with WSL, Chrome, Claude Code and OpenCode. **NEXUS** has no machine-specific paths and a documented install, but it has not yet been tried on a clean machine. **The harness** still assumes its author's machine: today it is a codebase to read, not a product to install.

---

## The tooling: `workflowy-nexus`

### NEXUS MCP server: the daemon, the broker and the eight tool families

**What it is.** An MCP server that gives any MCP-capable agent (Claude Code, OpenCode, Claude Desktop and others) a Workflowy account as structured, shared memory:

- **Every node is a Markdown document.**
- **Anchor Points** carry permanent IDs (`[ap:…]`) that survive renames and moves.
- **Anchor Docks** are containers.
- **Tags and slice groups** cut across the tree.
- **Any subtree, Dock or slice** renders as one Markdown document, and the human sees it too.

The surface is eight tools with 74 verbs, each tool with its own `describe`: `node` (17 verbs), `anchor` (17), `slice_group` (11), `beacon` (8), `system` (8), `tag` (6), `lightning_rod` (5), `glimpse` (2).

**What's underneath:**

- one daemon serves every agent on the machine;
- a write governor paces writes across the whole account;
- a coherence layer (a journal, an operation cache and barriers) keeps concurrent agents' writes consistent.

**Export archives** let a finished subtree be exported to a verified file and removed from Workflowy, while staying readable, searchable and renderable through the same tools (marked 📦). Thousands of nodes, and no sprawl.

- **Status:** working, used daily; every agent in its author's programs calls it. Version 1.0.1 (classified Beta), MIT license, 5,402 test functions.
- **Where:** `src/workflowy_mcp/`: about 118,000 lines, covering the server, a Workflowy REST client and the coherence layer. Console commands `workflowy-mcp` (the stdio shim, which starts the daemon on first call), `workflowy-mcp-shim` and `workflowy-glimpse-broker`.
- **Workflowy:** this *is* the Workflowy layer.
- **To run it:**
  - a Workflowy account and its API key;
  - Python 3.10+ with `uv`;
  - an MCP host pointed at `workflowy-mcp`, or any Streamable-HTTP host at the daemon's `/mcp` endpoint.

  The daemon runs natively and refuses to run inside WSL; agents in WSL reach it over HTTP. It is not on PyPI.

### The tool descriptions: the built-in skill

**What it is.** The tools teach themselves, so you can load the MCP tools and nothing else: no skill file and no agents file are required. The teaching comes in three layers:

1. a top-level server description;
2. eight family descriptions, always on;
3. **progressive disclosure** through `describe`: a family's index, or one verb's full mini-manual.

A failed call returns that verb's documentation.

- **Size:** 8 tools in 44,487 characters on the wire. That is about 11,000 tokens by a characters ÷ 4 estimate, and 61.5% smaller than the earlier surface of 43 separate tools (`scripts/measure_tool_surface.py`).
- **Status:** working. This is the first and main way an agent learns NEXUS.

### Cartographer and beacons

**What it is.** **Cartographer** mirrors a repository into Workflowy, one node per folder, file, class and function, so that code can be browsed, rendered and sliced like any other notes.

**Beacons** are small comment blocks that agents keep above the functions that matter: `@beacon[ id, slice_labels, kind, comment ]`.

- Each label becomes a Workflowy tag.
- A **beacon slice**, every symbol carrying one label shown as its signature plus its comment, is therefore one render.
- `beacon.snippet` reads any mapped function live from disk, by name.

**Disk is truth:** the map is a projection, refreshed from disk. Agents keep the beacons current as they work ("curate, don't carpet-bomb"), so each agent leaves the slices up to date for the next one. Each project keeps a **Slice Catalog**: its beacon tags, each with a description of the code it covers.

- **Status:** working, used daily. Both repositories are mapped. Every worker in a program reads code through slices and snippets, and the Conductor runs one governed map refresh per unit of work.
- **Where:** the repository-root modules: `nexus_map_codebase.py` (the mapper, about 10,500 lines) and the `beacon_*.py` modules. Plus the `beacon` tool family: snippet, resolve, check, fix, tag writes and map refresh.
- **Workflowy:** the map lives in Workflowy. The beacon grammar, the slices and the snippets work on disk, which is why the Agent Kit can run them without Workflowy.
- **To run it:** the NEXUS server, and a repository on the same machine.

### GLIMPSE Chrome extension and the Markdown modal

**What it is.** The human side of NEXUS, running on workflowy.com:

- Agents can **GLIMPSE** exactly what is expanded on the human's screen.
- The **docked Markdown modal** renders any subtree, Dock or slice group as one Markdown document, and agents can push their renders into it: click the Workflowy equivalent of a folder and read every document inside it as one page.
- It adds hotkeys (path copy, tag-prefix expansion), a node navigator with tab aliases so that an agent can target one particular tab, Cartographer menus and a jobs panel.

Everything stays local: the extension reads the page itself, not the API, and talks to the NEXUS server through a local broker protected by a shared-secret token.

- **Status:** working, used daily. Version 3.36.1, Manifest V3. It installs as an unpacked extension; it is not in the Chrome Web Store.
- **Where:** `workflowy-glimpse-cache/` (the folder name is historical): about 31,700 lines of first-party JavaScript.
- **Workflowy:** by nature.
- **To run it:**
  1. Chrome 111 or later; load `workflowy-glimpse-cache/` unpacked;
  2. start the NEXUS server (it launches the broker);
  3. paste the broker token into the extension's options;
  4. open workflowy.com.

### Agent Kit (the `code-map` skill)

**What it is.** In its README's words: *"A standalone skill, `code-map`, for any repository whose code carries `@beacon[...]` comment blocks."* It gives an agent in Claude Code or OpenCode beacon slices and live, line-numbered code reads straight from disk, with no MCP server and no Workflowy:

- a committed `code-map/CATALOG.md` lists the labels;
- generated `code-map/slices/‹label›.md` files show each slice;
- the agent keeps them current as a standing duty.

Commands: `init`, `labels`, `slice`, `read`, `tag`, `untag`, `check`. Its engine is generated from NEXUS's own Cartographer and beacon code. It is the code half of NEXUS, carried to repositories that will never see Workflowy.

- **Status:** en route. The first pass is built and its 626 tests pass. A stranger-style trial on a sample Python repository ran end to end with plain `python3` and nothing installed. Beacon blocks in JavaScript or TypeScript need tree-sitter: without it, `init` stops and prints the install command. Nobody outside this project has used it yet.
- **Where:** `agent-kit/` (9 files, about 8,800 lines); the skill itself is `agent-kit/.claude/skills/code-map/`.
- **Workflowy:** none, and its build enforces that: the build refuses any Workflowy or NEXUS wording under `agent-kit/`.
- **To run it:**
  1. Python 3.10+ and git;
  2. copy the skill folder into a repository;
  3. run `python .claude/skills/code-map/scripts/code_map.py init`.

> 📸 **SCREENSHOT — Agent Kit in a terminal:** a terminal in a small, invented Python sample repository (no real project code) after running the Agent Kit. **Top half:** `python .claude/skills/code-map/scripts/code_map.py slice ‹label›`, with its output showing two or three function signatures and their beacon comments. **Bottom half:** `python .claude/skills/code-map/scripts/code_map.py read ‹one of those functions›`, showing its line-numbered body. Crop to the terminal window. Crop out any home-directory path in the prompt, and make sure no Workflowy or NEXUS window is visible: this shot is about running without either.

### Ways to teach an agent: all optional, all lightweight

The only thing an agent needs is the MCP tools, and they explain themselves (above). For anyone who wants more, there are several forms, depending on taste:

- **A skill file: in development.** Five lines whose only job is to load one public guide: either a public Workflowy Anchor Point, or the same text as Markdown in this repository. [TODO Dan: if the agent guide and skill ship this week (plan item S1), link `skills/workflowy-nexus/SKILL.md` and `docs/agent-guide.md` here and change the status to "working".]
- **Agents files: working, written for this setup.** For OpenCode, the global worker instructions and the multi-model cast. For Claude Code, the same prompts, composed at launch. Both live in `nexus-harness`; see *Agents files* below.
- **A Workflowy subtree as the skill.** The idea underneath the whole harness: the skill is a Workflowy subtree, not a document on paper, and an agent loads it with one line, *"Read [ap:…] and follow it."* The two large ones are Manager Sessions and the Program Conductor Toolkit (next section). A small, public guide subtree is in development, with the skill file.
- **A packaged Workflowy agent: planned.** The pieces exist (the tool descriptions, the guide's text, a design for a skill bundled with the server), but they are not packaged.
- **The Agent Kit: en route** (above). It is a skill for code on disk, not for Workflowy.

---

## The harness, written in Workflowy: two subtrees

These two are instructions, not code. They live in Workflowy as subtrees, are read through NEXUS, and run on the harness software. That is why both READMEs describe them.

### Manager Sessions

**What it is.** A Workflowy subtree of instructions that turns an agent into the **Manager**: the human's thinking partner, and the operator of Conductor programs. Its own opening line: *"This is where the human thinks out loud with an agent."*

- Each conversation gets its own Anchor Dock.
- Durable material is captured as Anchor Points and later promoted into topic Docks.
- A Residue page and a Board carry what is active.
- **Curation** decides what comes along to the next session.

As the instructions themselves put it, *"There is absolutely nothing trivial about keeping organized when there's hundreds of sessions and ten different topics."* It is the knowledge-work half of the harness, and it runs in Claude Code or OpenCode.

- **Status:** working, used daily: more than 200 sessions since mid-September 2026. It is written for its author's own use and not yet shared publicly.
- **Size:** about 560 KiB of standing instructions: a 182 KiB root description, read by progressive disclosure, and eleven permanent pages.
- **Workflowy:** completely. Every session begins with an `anchor.dock.create` call, and the material still waiting for curation is itself a slice-group render (`recent: 'curated'`). Running programs also needs the harness's command line.
- **To run it:** Workflowy, NEXUS and a Manager prompt (in `nexus-harness`); program management also needs the harness scripts.

> 📸 **SCREENSHOT — the Manager starting a session:** the Manager App on the desktop, Manager chat pane, at the start of a session about NEXUS or this portfolio. Visible: the human's opening line, then the Manager's first tool calls (the `node.read` of the Manager Sessions description and the `anchor.dock.create` of its session Dock), collapsed so that only their call headers show, then the Manager's first reply. **Privacy:** the description's text must not be visible (its first screen names private topics); the program sidebar shows only NEXUS and portfolio programs; crop the browser chrome and any URL.

### Program Conductor Toolkit

**What it is.** In its root's words: *"Reusable, program-independent initialization for a CONDUCTOR (the meta agent) that runs implement → audit → remediate programs through Workflowy Anchor Points, with worker agents dispatched by socket."* A Conductor:

1. plans a program;
2. writes a **socket** for each worker: an Anchor Point holding the worker's dispatch, meaning its role, its inputs, what it may write and its contract;
3. launches the workers on many models;
4. reads their results, runs audits and remediation;
5. hands off between its own sessions.

All of it happens through Workflowy. *"There is only one prompt and it never changes."* The Toolkit is the source-code half of the harness. It runs knowledge-work programs too, like the one that produced this page.

- **Status:** working, used daily. It is written for its author's setup (a named model cast, OpenCode and Claude Code mechanics) and not yet shared publicly. [TODO Dan: if the Toolkit's Markdown export ships this week (plan item S2), link `docs/toolkit/` in `nexus-harness` here.]
- **Size:** 1.7 MiB across about 145 nodes:
  - the Conductor's initialization and the dispatch schema;
  - the program interface and the recipe blocks;
  - 25 worker clauses (seats, helpers, reconnaissance);
  - doctrine (audit bars, human-readable reporting).
- **Workflowy:** completely:
  - a single clause launches one governed whole-repository `beacon.map.refresh` and polls `system.job.status`;
  - workers read code by beacon slice and `beacon.snippet`;
  - briefs, sockets, ledgers and reports are all Anchor Points.

  It also runs on the harness: seats are forked with `oc_seat`, map refreshes go through `oc_map_refresh`, and every worker measures its own context with `context_status`.
- **To run it:** NEXUS, the harness, Claude Code and/or OpenCode, and model accounts.

> 📸 **SCREENSHOT — the Toolkit as a Workflowy subtree:** Workflowy zoomed into the Program Conductor Toolkit Dock (`f382b87e`), its top-level children visible: the initialization page, the dispatch schema, the program interface, the BLOCKS shelf and the WORKER CLAUSES shelf, opened one level. Optionally, beside it, the docked Markdown modal showing one worker clause rendered as Markdown. **Privacy:** crop the left sidebar and the starred list; check every visible title and tag for anything from your work (the breadcrumb included).

---

## The harness software: `nexus-harness`

### Manager App: the web app, on desktop and phone

**What it is.** The cockpit beside the agents, on the desktop and on the phone:

- **Dictate** into a persistent editor, **Refine** the dictation with a frontier model that reads the conversation as context, and **Send** it to the Manager.
- **Read** the Manager's conversation live (Claude Code or OpenCode) and inspect any tool call.
- **See every program**, Conductor and worker as a tree, with live status, context use and wall-clock telemetry.
- **Open any Workflowy view** (a Dock, a slice group, a render) in a **View** pane, rendered exactly as the extension's Markdown modal would show it.

The phone page, `/m`, has four tabs (Chat, Programs, Context and View) and works over a private Tailscale network. The app began as the "Transcription Pane", and its folder keeps that name.

- **Status:** working, used daily. Its own README is a detailed feature log rather than an introduction; the harness README is the introduction.
- **Where:** `transcription-pane/`: 181 files, about 71,600 lines, including 37 pytest files (599 tests) and 33 `node --test` files with headless-Chrome DOM fixtures.
- **Workflowy:** light. Only the View pane talks to Workflowy: it is an MCP client of the NEXUS daemon, calling the same tools the agents call. The rest of the app does not touch Workflowy.
- **To run it:** WSL and Python 3 (the server uses only the standard library); `bash transcription-pane/start.sh`, then `http://127.0.0.1:9911`.
  - Claude mode needs Claude Code and the Claude Code host;
  - OpenCode mode needs OpenCode, CodeNomad and the CodeNomad extension;
  - the paths still assume its author's machine.

### Claude Code host

**What it is.** In its documentation's words: *"a private local owner of the official Linux Claude Code CLI, not an inference proxy, terminal driver, pane server or replacement OpenCode client."* It starts and supervises official Claude Code sessions (the Manager, Conductors and lead seats) on the user's own Claude login:

- **exact send receipts** and replayable records;
- **native controls** (interrupt, model, effort), each confirmed by reading back the applied setting;
- **one recovery** after an explicit capacity error;
- **an honest halt** at a usage limit: *"⛔ Halted: Max usage limit reached … Nothing resumes by itself."*

It serves the conversation to the Manager App. Around it sit Claude seats driven through the same `oc_*` commands, the prompt composers (with a test that catches drift from the OpenCode prompts), and `context_status` for Claude Code.

- **Status:** working, in daily use, young (October 2026). This page was drafted by a seat running on it.
- **Where:** `scripts/claude_headless/` and the `claude_host*` and `oc_claude*` scripts: 31 files, about 13,900 lines. Documentation: [`docs/claude-headless-host.md`](https://github.com/daniel347x/nexus-harness/blob/main/docs/claude-headless-host.md) and [`docs/claude-headless-manager.md`](https://github.com/daniel347x/nexus-harness/blob/main/docs/claude-headless-manager.md).
- **Workflowy:** every session it launches gets the Workflowy MCP attached. Its transport and supervision core is Workflowy-free.
- **To run it:** WSL, the official Claude Code CLI with a login, and `scripts/claude_host.sh start`.

### Manager CLI and the seat system (the `oc_*` scripts)

**What it is.** The Manager's command line, run through one launcher, `oc.sh ‹name›`:

- **the program view**, `oc_view`: every running program, Conductor and worker at a glance, and a program's state pushed into the Workflowy Markdown modal;
- `oc_steer` (message a running agent) and `oc_quick` (a whole quick program in one command);
- `oc_peek` and `oc_search`: read and search any session's history, OpenCode or Claude;
- `oc_telemetry`: context and wall clock per phase, for every worker;
- `oc_archive_cut`: archive a finished wave of work.

**The seat system**, `oc_seat` on `oc_pack`, builds **seat bases**: parked sessions pre-loaded with a program's shared reading. It then **forks** them, so that lead seats and helpers start warm instead of reading everything again.

- **Status:** working, used daily.
- **Where:** `scripts/`: 33 files, about 20,300 lines. The `scripts/` folder as a whole carries 71 test files with 1,214 test functions.
- **Workflowy:**
  - the program operations call the NEXUS daemon: seats, quick programs, map refresh, archiving;
  - the observation tools do not: telemetry and search.
- **To run it:** WSL, the OpenCode service and/or the Claude Code host, and the NEXUS daemon; then `bash scripts/oc.sh view`.

> 📸 **SCREENSHOT — the program view in a terminal:** a WSL terminal showing `bash scripts/oc.sh view` while a program runs: the program, its Conductor and its workers, one row each, with their status. **Privacy:** only NEXUS and portfolio programs may appear (archive or wait out any other); crop the prompt's path and any session ids you would rather not show.

### OpenCode layer: the proxy, the plugins, the config, restart and upgrade

**What it is.** What makes OpenCode a dependable host for long-running swarms:

- **a payload proxy** in front of every model provider (`proxy/`, standard-library Python): Anthropic's prompt-cache lifetime under control, backoff on rate limits and server errors for about 25 minutes before OpenCode sees an error, and a request log;
- **five OpenCode plugins** (`plugins/`):
  - `context-status.ts`, the `context_status` tool every agent uses to know how full it is;
  - `tool-output-stub.ts`, which saves oversized tool output behind a stub;
  - `mcp-fetch-timeout.ts`, so that long, silent MCP calls survive;
  - `chatgpt-cache-affinity.ts`, so that the ChatGPT plan's prompt cache hits;
  - `openai-usage-tap.ts`, a usage log;
- **the master config**, with auto-compaction off: agents hand off or land by measured context, never by summarising themselves;
- **a restart script** that checks a candidate config on a throwaway server before it touches the live one, and **an upgrade script** that checks the new OpenCode source against the layer's contracts and can roll back.

- **Status:** working, used daily on the OpenCode side.
- **Where:** `proxy/`, `plugins/`, `config/`, and the restart and upgrade scripts in `scripts/`.
- **Workflowy:**
  - the proxy has none;
  - one plugin special-cases Workflowy tool output;
  - the config has one entry for the Workflowy MCP.

  The layer stands alone easily, and is useful to OpenCode users who don't care about Workflowy.
- **To run it:** OpenCode as a WSL service, and provider keys.

### Agents files

**What it is.** The prompts behind the swarm:

- **`agents/AGENTS.md`**, the global instructions every OpenCode worker receives. Its first half, "Working in Workflowy — the essentials", is in effect a Workflowy skill.
- **`agents/agent/*.md`**: 71 files, the **cast**, one per model and thinking level across several providers, plus the Manager and Conductor prompts.
- **Claude Code** has no `CLAUDE.md`, by design. Its prompts are composed at launch from these same files (`scripts/claude_headless/seat_prompt.py`, `manager.py`), and a drift test keeps the two runtimes in step.

- **Status:** working, in daily use. The files are model wrappers for this setup, not drop-in files for another project.

### CodeNomad extension

**What it is.** A Chrome extension on CodeNomad, a third-party web client for OpenCode:

- it reports to the Manager App which session, model and thinking level is on screen, so that Send reaches the right session;
- it improves the conversation's spacing;
- it shows tool calls with previews, recognising Workflowy calls by their family.

- **Status:** working, used less since the Manager moved to Claude Code inside the Manager App. Pinned to CodeNomad 0.20.1.
- **Where:** `chrome-extension/codenomad-session-watcher/`: 25 files, about 3,200 lines, with its own tests and [README](https://github.com/daniel347x/nexus-harness/blob/main/chrome-extension/codenomad-session-watcher/README.md).

---

## How the pieces depend on Workflowy

A fair question about the harness: what does it have to do with Workflowy? Nothing, except that it is built to use it. The dependency is real, but it sits in a few clear places.

| Component | Needs Workflowy? | Where |
|---|---|---|
| NEXUS server, Cartographer, GLIMPSE extension | Yes, by nature | They are the Workflowy layer |
| Manager Sessions, Program Conductor Toolkit | Completely, and they also need the harness | Written in NEXUS calls throughout; they run on the harness's scripts (`oc_seat`, `oc_quick`, `oc_map_refresh`) and `context_status` |
| Manager CLI and seats | For program operations | Seats, quick programs, map refresh and archiving call the daemon; telemetry and search do not |
| Claude Code host | Thinly | It attaches the Workflowy MCP to each session it launches |
| Manager App | Lightly | Only the View pane, as an MCP client of the daemon |
| OpenCode layer | Barely | One config entry; one plugin special-cases Workflowy output |
| Agent Kit | **No**, enforced by its build | Beacon slices from disk alone |

---

## Retired

- **GLIMPSE in Workflowy's desktop app.** GLIMPSE once also ran inside Workflowy's desktop (Electron) app, by injection. Retired in late May 2026; the patcher, `glimpse_deploy_trampoline.py`, remains at this repository's root as history.
- **The transcription widget for TypingMind.** The Manager App grew out of an earlier dictation-and-refine widget that ran inside TypingMind, retired as a daily driver when the Manager moved to OpenCode and then to Claude Code.
- **The first skill-as-a-subtree page.** An earlier Workflowy page that taught agents GLIMPSE is retired. Its content lives on in `agents/AGENTS.md`, and it seeds the guide that is in development.

---

## Who made this

One person, not a company. Daniel Nissenbaum (Dan) is an individual contributor, formerly a principal at Microsoft for five years. In his own words he has been *"never that active in open source, incredibly active in private source"*. Not a good marketer, he says, and for a simple reason: *"Just holed up, non-stop working with these agentic systems and finding ways to make them work the best, and this is what I've landed on."* Now he has *"something that I'd like to share."*

**Agents wrote the code; Dan drove every idea and insisted on good design.** The agents built the system with the system itself, which makes it a fully agentic supporting system. This page is one example: it was drafted by an agent seat in a Program Conductor Toolkit program running on the Claude Code host, working from a components inventory that another agent compiled by reading both repositories.

**Credit.** One starting point was not Dan's: a minimal Workflowy MCP wrapper, [`vladzima/workflowy-mcp`](https://github.com/vladzima/workflowy-mcp) (MIT) [CONFIRM Dan: vladzima/workflowy-mcp]. By his estimate it is about one percent of what NEXUS is now, and NEXUS still carries its package and command names (`workflowy_mcp`, `workflowy-mcp`). Everything else was built here, with no framework underneath.
