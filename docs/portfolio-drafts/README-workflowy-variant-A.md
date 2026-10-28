# NEXUS · Workflowy, agent-enabled

**Your agent sees what you see. You see what your agent sees. Workflowy is the shared memory between you, for your knowledge work and your source code alike.**

NEXUS is an MCP server and a Chrome extension. Together they turn a Workflowy account into a single shared canvas for a human and any number of AI agents. Every bullet is a unit of memory: notes and decisions, plans and to-dos, the record of a long project, and (mapped live from disk) every file, class and function in your repositories. An agent reads exactly the part of that canvas you point it at, writes back into it, and cites what it wrote by a permanent handle. You read the same tree, in Workflowy, as one rendered document.

> 📸 **SCREENSHOT — hero, "one tree, two readers":** Chrome, full width, workflowy.com open on a project Dock (an Anchor Dock with 6–10 captured Anchor Points visible, emoji-led titles, a few `[ap:…]` handles showing). The docked Markdown modal is open on the right, showing that same Dock rendered as one clean Markdown document (headings, a table, a code block). If space allows, a narrow terminal on the left with Claude Code or OpenCode just after a `glimpse` or `anchor.dock.render` call, its output matching the modal. Crop to the browser plus the terminal sliver; no personal Docks or private topics in view.

---

## In one breath

> *"If Workflowy didn't exist, it would have to be invented."*

Context management is the defining problem of working with agents: giving each agent the relevant material, not too much and not too little, keeping that material current, and handing results from one agent to the next. The usual answer is a growing pile of Markdown files. NEXUS answers it with a node graph instead: an outline in which every node is a Markdown document, tags cut across the whole tree, order is whatever you set by moving a bullet, and the visualisation layer already exists, because it is Workflowy.

Picture the nodes as points in space. A set of them lights up for one agent, then another set for another agent, then they go dark again. *"You just see all these little dots of light, kind of, like, lighting up and vanishing. That is context management."* NEXUS is the machinery that lights the dots, and Workflowy is where you watch them.

---

## The mirror image

The tooling is half for knowledge work and half for source code, *"in both cases deeply integrating with Workflowy."*

| | **Knowledge work** | **Source code** |
|---|---|---|
| **The tooling (this repository)** | **Anchor Docks** (containers) and **Anchor Points** (captured, citable notes); **slice groups** that cut across them by tag; **GLIMPSE**, which renders any of it as one Markdown document; the **docked Markdown modal** in Workflowy | **Cartographer**, which maps a repository into Workflowy one node per folder, file, class and function; **beacons**, comment blocks in the source whose labels become tags; **beacon slices**; live **snippets** of any function by name |
| **The agentic harness built on it** | **Manager Sessions**: an agent that thinks, plans and remembers with you, session after session | **The Program Conductor Toolkit**: a Conductor agent that runs swarms of worker agents to plan, implement, audit and remediate code |

The harness on top has the same two halves as the tooling underneath. The Manager is the knowledge-work side: thinking, planning, curating memory. The Conductor is the source-code side, running agentic swarms (it runs knowledge-work programs too). *"Because in order to do effective source code, you have to also be able to strategize the work."* That makes it a system for architects, planners, founders, and people who are not primarily programmers but have technical projects to run.

**All of it is Workflowy-centric.** The memory, the instructions, the plans, the swarm's dispatches and reports, and the map of the code all live in one outline that the human can see.

> 📸 **SCREENSHOT — "both halves in one outline":** Workflowy, collapsed to show at one level a knowledge Dock (e.g. a session Dock with captured notes) and, beside it, a mapped repository node (🗺️ folder → 📄 file → class → functions with 🔱 beacon markers and `#tags` visible). Expand just enough of each to read three or four children. Caption idea: "The same outline holds what you decided and the code you decided it about."

---

## Why Workflowy

This isn't a Workflowy plugin that happens to be useful for agents. The argument runs the other way: once you know what agents need, a node-based outliner turns out to be the ideal substrate for them, and Workflowy already is one.

**One document, and the constraint is a feature.** Workflowy holds everything in a single tree. It is far more opinionated than a file system, which is *"too powerful. It's impossible to manage"* (which disk, which folder, which of several writers). *"Sometimes the constraints are as powerful as the flexibility."*

**A folder you can read as one document.** A subtree is a folder with subfolders, and its nodes are the files. In Workflowy (with NEXUS's renderer) you click that "folder" and read everything inside it as one continuous Markdown document. No Markdown tool does that, not even with transclusions and backlinks. Tags then filter what the document shows, dynamically, without creating a new document.

**Order is yours.** Move a bullet and the render order changes. A file system sorts by name or date; you can't just set an arbitrary order. Indenting, outdenting and reordering take a fraction of a second, and you see where things live and what they say at the same time.

**The data and its view are one thing.** *"You can't separate the database from the way the humans view the database."* The beacon map could have lived in SQLite, but then nobody could see it, and the project becomes a database project that sooner or later demands a visualisation tool built on top. Workflowy is a visualisation layer over its own cloud data layer, *"and it comes for free."*

**Thousands of nodes is a healthy system.** *"Thousands of nodes in Workflowy is par for the course. That's a sign of a good system... whereas thousands of Markdown files seems like a sprawl."* And there is no such thing as tagging files in a way an agent harness can use: *"if you wanted to build a system to tag files, you'd have to build something like Workflowy to do it. Why not just use Workflowy?"*

### Where this sits today, honestly

The industry agrees on the problem. Anthropic calls it *context engineering*, "curating and maintaining the optimal set of tokens," and names *context rot* as the reason it matters. Coding agents have converged on curated instruction files (`CLAUDE.md`, `AGENTS.md`, rules folders) loaded progressively, plus compaction and subagents. Consumer assistants keep a memory they synthesise for you, which you can correct but did not write. The pains are well documented: files that grow and never get trimmed (*"the LLM never trims, only amends"*), staleness, contradictions between files, and instructions summarised away by compaction.

NEXUS takes a different position on two points. **The memory the agent reads is the page you read**: a Workflowy bullet, not a hidden store or a file you open in an editor. And **a human chooses the context**: a slice group hands an agent an exact, cross-cutting set of nodes, selected by tag and rendered as one document. Elsewhere, selection happens by file path, by folder, by the model's reading of a description, or by similarity search.

To be fair to the neighbours: Workflowy now ships a desktop MCP of its own, and Tana offers one over its nodes and supertags, so "an agent can read and write your outliner" is not the new part here. What is new is the system built on top. Letta keeps agent memory as git-versioned Markdown shared across agents, which is close in spirit. Nobody, this project included, has published numbers showing that tag-selected context beats curated files. The claim here is a design claim, backed by daily use.

---

## The knowledge-work half

**Anchor Docks are where things go; slice groups are what you look at.**

- **Anchor Docks** are named containers in your outline: a project, a person, a stream of meeting notes, a to-do list.
- **Anchor Points** are what agents capture into them. Each is a node whose note is a full Markdown document, optionally carrying a permanent handle at the front of its title, like `[ap:K7M2QF]`. The handle survives renames and moves, and every tool accepts it. Prompts, reports and other notes cite memory by handle, so a link never breaks when you reorganise.
- **Slice groups** are saved views: a root, a family of tags, and a policy for what to expand, suppress or deprecate. Tag a node and it joins the view; render the view and an agent (or you) reads exactly that set of nodes, from anywhere in the tree, as one document.
- **GLIMPSE** is the act of reading. With no arguments it reads *what you have open in Workflowy right now*: your zoom, your expansions, your search. *"Human: you never describe context; you point at it. Agent: you never guess context; you look."*

Capture is fast and needs no filing decisions from you: the agent picks the Dock, captures, and moves on. Curation comes later and is just tagging, and Workflowy's drag-and-drop does the rest. Finished subtrees can be exported to a verified archive file and deleted from Workflowy, and they stay readable and searchable, marked 📦.

A typical exchange, as an agent sees it:

```text
anchor(verb='point.capture', params={'parent_handle': '[ap:Q3PLAN]',
       'name': '🧭 Decision: ship the importer behind a flag',
       'content': '…Markdown…', 'ap_id': 'auto'})
glimpse(verb='run', params={'group_handle': 'Importer', 'recent': '6h'})
```

The first call captures a decision into a Dock. The second reads everything in the "Importer" slice from the last six hours, wherever in the tree it lives.

> 📸 **SCREENSHOT — capture lands where you can see it:** Split view. Left: a terminal with an agent's `anchor.point.capture` call and its acknowledgement (new `[ap:…]` handle visible). Right: Workflowy with the target Dock expanded and the new bullet at the bottom of its children, its handle matching. Keep the content neutral (an open-source design note).

---

## The source-code half

A **beacon** is a small comment block that agents keep above the functions that matter:

```python
# @beacon[
#   id=myproj@load_rows,
#   slice_labels=myproj-ingest,
#   kind=ast,
#   comment=Reads one batch of rows and validates them before anything is written.,
# ]
def load_rows(path: str) -> list[dict]:
```

**Cartographer** maps the repository into Workflowy: one node per folder, file, class and function, with each beacon's labels as tags and each function's docstring and comments as its note. **Disk is truth**: the map is a projection, refreshed from disk, and nothing is ever edited the other way round.

So a **beacon slice** (every symbol carrying a label) is one render: signatures plus the comments their authors wrote, from any number of files, with no bodies and no file reads. A 15,000-line file shrinks to a few pages an agent reads in one pass, after which it knows exactly which function to open. `beacon.snippet` then reads any mapped function live from disk by name, with file line numbers, ready to edit.

Agents keep the beacons current as they work, as a standing duty: *curate, don't carpet-bomb*. They do this remarkably well, so documentation and code drift much less, and a fresh agent understands a large codebase quickly. A slice group over beacon tags is a menu of the codebase's topics, which in this system is called a **Slice Catalog**. When an agent here thinks *file search*, it thinks Slice Catalog first.

> 📸 **SCREENSHOT — a beacon slice rendered:** The docked modal showing one beacon slice of this repository (a tag with roughly 10–20 symbols, e.g. the beacon or Cartographer topic), headings visible as file paths, then `🔱 def …(…)` signatures with their comments beneath. Optionally, inset: the same function's beacon block in an editor. Caption: "Comments in the source, maintained by agents, become a map any agent can read."

---

## The Markdown modal: one document for both of you

Most of this system's value shows up in one place. NEXUS adds a panel docked to the right edge of Workflowy, the **Markdown modal**. It renders whatever part of your outline you choose as one clean, scrollable Markdown document, with headings, tables and code.

What it shows is exactly what an agent would get if it read the same view. Point it at a Dock and you read the Dock. Apply a slice group and you read every tagged node, gathered from across the tree, as one document. An agent can also push a render into it ("render that in the modal"), so you read what the agent assembled without it spending a token on you. Read the other way, the agent can GLIMPSE the modal and get back exactly what you are looking at.

A few details make it practical. **Freeze** captures a view so you can collapse and tidy the outline while still reading the snapshot. A **timestamp sort** reads any slice as an append-log ("what happened, in order"). Click any row, and the document re-roots there.

> 📸 **SCREENSHOT — the modal, introduced:** Workflowy with the modal docked right, a slice group applied (green APPLY clicked in the widget). Visible: the split panel (the flat node list on top, the rendered document below), the ribbon's 🔒 Freeze and sort controls, and a rendered page with at least one H1/H2, a list and a code block. Neutral content (e.g. NEXUS's own documentation). Crop so the outline on the left and the modal on the right are both legible.

> 🎬 **OPTIONAL GIF (≈20 s) — "click a tag, read the slice":** In the widget, click APPLY on a slice group → the modal fills with the rendered slice → click two rows in the modal's top list → the document re-roots each time → click 🔒 Freeze, then collapse the outline: the document stays put.

The extension also adds a few keyboard gestures for power users. **F3** copies a node's path and handle for pasting to an agent. **F8** expands every node carrying a slice's tags. **Ctrl+F12** opens the Cartographer menu for refreshing a mapped file or folder from disk. A small **widget** shows every running Cartographer job, refreshes the cache, finds any handle an agent mentioned, and lists your Anchor Docks with a **Render** button on each.

> 📸 **SCREENSHOT — the widget:** The in-page widget expanded: the job panel (one completed refresh), the "Refresh cache" row with the **Anchors** button, the Anchors popup open beside it showing favourite Docks with ⭐ and [Render] buttons. Crop tight to the widget and popup.

---

## What it makes possible: swarms in the same tree

The Manager and the Conductor are written *as Workflowy subtrees*, not as files: instruction systems that agents load by handle and read by progressive disclosure. The prompt that starts a Conductor never changes: *"You are the CONDUCTOR. Read [ap:QE4RXD] and follow it."* Everything else (the program's goals, each worker's dispatch, each result, each audit) is an Anchor Point in the same tree, which you can open and read while the swarm runs.

- **Manager Sessions** gives each conversation its own Dock. Durable material is captured as it comes up and curated later into topic Docks, so the next session picks up the thread without you reconstructing it. It is in daily use and has run through more than two hundred sessions.
- **The Program Conductor Toolkit** runs programs. A Conductor writes a *socket* for each worker (an Anchor Point holding its role, inputs, write permissions and contract), launches workers on OpenCode or Claude Code, reads their results, and runs audits and remediation. Workers read code through beacon slices and snippets, and write back into Workflowy.
- **The Manager App** is the cockpit beside it all, on desktop and phone: dictate, refine and send to the Manager; watch every program and worker live; open any Workflowy view. It lives in its own repository: see **[the Manager App README]([TODO Dan: Manager App repository URL])**.

> 📸 **SCREENSHOT — a program running (cross-screenshot):** The Manager App desktop view with the programs pane open: one program (an open-source program, e.g. this portfolio program) showing its Conductor and several workers with live spinners. If possible, a second panel or inset of Workflowy with that program's root Dock expanded, showing the worker sockets (`⚖️ DISPATCHED · RESULT …` titles) and one finished `🗣️ For the human` child. Nothing from work topics in view.

> 📸 **SCREENSHOT — the View pane (cross-screenshot):** The Manager App's View pane rendering a Workflowy slice group, the same document the modal shows, on the phone page (`/m`). Shows that one render funnel serves the extension, the agents and the app.

The integration with OpenCode and Claude Code is a strength, not a detour: Workflowy becomes the coordination surface for agent swarms. The instruction subtrees can be published as Workflowy shares, with a Markdown export committed for anyone who doesn't use Workflowy: [TODO Dan: public links to the Manager Sessions and Toolkit copies, and their committed exports].

---

## Built by agents, with this system

Agents wrote the code. A human drove every idea, insisted on good design, and kept the project moving. *"This is a fully agentic supporting system,"* and the system you are reading about is the one that built it. The later work was planned in Manager Sessions and executed as Conductor programs, whose workers read the code through beacon slices and reported back as Anchor Points. This README was drafted the same way.

What a visitor will find in this repository, measured at the time of writing:

- **≈168,000 lines of first-party code**: about 118,000 in the server package, 19,000 in the Cartographer and beacon modules, and 32,000 in the Chrome extension.
- **5,402 test functions.**
- **1,292 commits since January 2026.**
- **8 MCP tools exposing 74 verbs**, collapsed from 43 separate tools. The always-on tool surface is ~44,500 characters (≈11,000 tokens), **61% smaller** than the legacy surface.

---

## Who made this

NEXUS isn't a company product. It was made by one person, **Dan Nissenbaum**: an individual contributor, formerly a principal at Microsoft for five years, *never that active in open source, incredibly active in private source.* He is not a marketer either; he has spent the past months *holed up, working non-stop with these agentic systems and finding ways to make them work the best,* and this is what he landed on.

Apart from Workflowy itself and its API, there was one starting point: a minimal example Workflowy MCP wrapper [CONFIRM Dan: vladzima/workflowy-mcp], roughly one percent of what is here. Everything else, the ideas and the design, is his, and agents built it.

[TODO Dan: contact / LinkedIn / portfolio link]

---

## Teaching an agent the system

**The tools are the skill.** You could load the MCP server and stop there. The server describes itself: a top-level description, eight always-on family descriptions, and `describe` on every family for progressive disclosure, down to a full mini-manual per verb. A failed call returns that verb's documentation. No skill file is required.

```text
node · anchor · glimpse · beacon · tag · slice_group · lightning_rod · system
family(verb='describe', params={'detail': 'index'})                  # what a family can do
family(verb='describe', params={'of': 'sift', 'detail': 'full'})     # one verb, in full
```

Lighter options for those who want more are on the way: a small public Workflowy subtree that an agent loads as its first prompt (the skill itself living in Workflowy), a short skill file pointing at it, and agents files for Claude Code and OpenCode. These are planned, not yet published. See [COMPONENTS.md](COMPONENTS.md).

---

## What's in this repository

A short orientation. Every component, with its status and where to read more, is listed in **[COMPONENTS.md](COMPONENTS.md)**.

| Component | What it is | Status |
|---|---|---|
| **NEXUS MCP server** | The daemon, stdio shim and local broker; the eight verb families | Working, used daily |
| **Cartographer and beacons** | Repository → Workflowy map; beacon slices; live snippets | Working, used daily |
| **GLIMPSE Chrome extension** | The Markdown modal, the widget, the hotkeys; reads the live page for agents | Working, used daily; a UI redesign is planned |
| **Agent Kit** (`agent-kit/`) | Beacon slices and live code reads from disk alone, no Workflowy | En route: built and tested (626 tests), not yet used by anyone outside the project |
| **The skill subtree and skill file** | The public, loadable form of the instructions | Planned |

**The other repository:** the Manager App, the Claude Code headless host, the `oc_*` command line and seat system, and the OpenCode integration. See [the Manager App README]([TODO Dan: Manager App repository URL]). **The two Workflowy subtrees** (Manager Sessions and the Program Conductor Toolkit) belong to both.

---

## Trying it

An honest note: this is in daily use by its author, and installation is still being smoothed for others.

- **You need** a Workflowy account and API key, Python ≥ 3.10 with `uv`, an MCP-capable host (Claude Code, OpenCode, Claude Desktop…), and Chrome for the extension.
- **The server**: `uvx --from git+https://github.com/daniel347x/workflowy-nexus-mcp-tools.git workflowy-mcp` (the stdio shim, which starts the local daemon on first call). HTTP-native hosts can dial the daemon directly. The daemon runs natively on Windows, macOS or Linux; agents inside WSL connect to the Windows daemon.
- **The extension**: load `workflowy-glimpse-cache/` unpacked in Chrome, paste the broker token into its options page, and open workflowy.com. Chrome Web Store publication is the next milestone.
- Configuration reference: [`CONFIG_REFERENCE.md`](../../CONFIG_REFERENCE.md). [TODO Dan: confirm the install steps above against a clean machine before publishing; refresh the extension's INSTALL.md (it still names an older version).]

---

## Licence and credits

MIT. Workflowy and its API belong to Workflowy; this project is independent of it. [CONFIRM Dan: credit line and MIT notice for vladzima/workflowy-mcp, if confirmed as the starting point.]
