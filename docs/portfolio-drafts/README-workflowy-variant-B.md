# NEXUS

### Workflowy, agent-enabled: one shared outline where you and your agents think, plan, remember and write code together.

> **Human:** you never describe context; you point at it.
> **Agent:** you never guess context; you look.

NEXUS is a Workflowy-native context-management system for agents and humans working together. Workflowy is the single shared canvas (projects, decisions, communications, notes), and source code is mapped live onto it from disk by Cartographer. The agent reads and writes the same tree the human sees: no copies, no exports, no drift.

In practice it is an **MCP server** (eight tools any MCP-capable agent can call: Claude Code, OpenCode, Claude Desktop and others), a **Chrome extension** that runs on workflowy.com, and **Cartographer**, which maps a code repository into the same outline. On top of it runs a complete agentic harness, built the same way and living in the same tree: a system for thinking and planning with an agent, and a system for running swarms of agents that write code.

*Built by one person with agents, inside the system it describes. [Who made this ↓](#who-made-this)*

> 📸 **SCREENSHOT — hero, "the outline and the document":** Chrome, full window, workflowy.com signed in, bookmarks bar hidden. Left: Workflowy zoomed into the node **"🪟 The Docked Markdown Modal"** (`ebea800d`), its children expanded one level so their titles show. Right: the docked Markdown modal open (the widget's 📌 Dock button), **not** frozen, rooted at that same node: the flat node list in the top half, the rendered document in the bottom half, with its H1 and two or three H2 sections visible. The breadcrumb trail at the top of Workflowy may show; nothing outside this node should. Widget collapsed. 16:9 crop. (The modal is rendering its own documentation: the point of the shot is *one outline, one document*.)

| | |
|---|---|
| **8 MCP tools, 74 verbs** | the whole agent-facing surface: more than forty earlier tools collapsed into eight verb families |
| **≈ 11,000 tokens** | the always-on tool surface an agent loads (schemas and descriptions: 44,487 characters, estimated at four per token), 61% smaller than the forty-odd tools it replaced |
| **≈ 168,000 lines** | first-party code: the server 118K, Cartographer and beacons 18.8K, the Chrome extension 31.7K |
| **5,402 test functions** | in this repository alone |
| **1,292 commits** | since January 2026 <sub>[TODO Dan: keep this row only if the public repository keeps its history]</sub> |
| **in daily use** | 211 Manager sessions so far, and the multi-agent programs that build the system itself, including the one that drafted this README <sub>[TODO Dan: keep the last clause only if the published README still came out of that program]</sub> |

---

## Two ways in

**If you have never used Workflowy,** you probably still care about the problem this solves. Getting the right context in front of each agent, not too much and not too little, is the central difficulty of working with agents, for knowledge work and for code alike. NEXUS is an opinionated answer to it: memory made of nodes and tags, which you and your agents share and which you can *see*, with a harness for agent swarms built on top. It runs on Workflowy because Workflowy turns out to be the best substrate I know for that idea. [Why Workflowy](#why-workflowy-if-it-didnt-exist-it-would-have-to-be-invented) makes the argument. Workflowy is an outliner: one infinitely nestable document of bullets, each of which can carry a note, and any of which you can zoom into, tag, move or share.

**If you already live in Workflowy,** you have probably sensed that it should be where your agents work too, and found no framework for it. This is that framework. It makes your Workflowy fully agent-enabled on both sides you need: your knowledge-work life, and your source code.

---

## The problem: context

Context management (prompt engineering, memory, agents getting the relevant material, not too much and not too little) is foundationally *the* challenge of using agents well. It is the driving force behind every agentic harness. The industry now says so in as many words: Anthropic calls it [context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents), *"curating and maintaining the optimal set of tokens"*, and researchers have measured [context rot](https://research.trychroma.com/context-rot): models grow less reliable as their input grows.

Auto-summarization and compaction help, but they are not the real solution. The real solution, and the one every coding agent has converged on, is **curating your Markdown files**: `CLAUDE.md`, `AGENTS.md`, rules folders, skills, memory folders. And there it gets hard. You end up with a sprawl of Markdown files: hard to know which to include for which agent, hard to keep current, and hard to see. As one practitioner put it, *"the LLM never trims, only amends."* A file can carry tags in its frontmatter, but those tags are metadata for some viewer to interpret. They do not hand an agent a human-chosen set of notes as one document.

The ideal substrate for this is a **node-based, tag-based system**, because nodes are units of memory, and only a certain subset of them is relevant at any given time. The picture I keep coming back to:

> *If the nodes were out in outer space, and a bunch of them lit up for one agent, and then another set of them lit up for another agent… you'd just see all these little dots of light, lighting up and vanishing. **That is context management.***

You can't do that with files. A database can store it, but has no tags, and nobody can see it. NEXUS does it in Workflowy, where the dots of light are bullets you can read:

- every memory is a **node**, and nodes carry permanent handles (a short ID, or an Anchor Point's `[ap:K7M2QF]`) that survive renames and moves, so prompts, plans and reports cite memory by handle, never by a path that breaks;
- a **tag** puts a node into a **slice group**, a saved view across the whole outline, and the slice renders for the agent as one Markdown document;
- you see exactly the same document in a panel beside your outline, so the context an agent is working from is never a mystery.

It is fully Markdown. Every note is a Markdown document, renders as Markdown and exports to disk, but with all the benefits of tags.

---

## What it looks like

The extension adds one thing to Workflowy that matters more than anything else: **a panel docked beside your outline that shows a document.**

Point it at any part of your Workflowy (one branch, a whole project, everything that carries a tag) and it renders that selection as **one continuous, readable Markdown document**, however many bullets it is made of, in the order you arranged them, with headings that stay sensible however deep the outline goes. You read a project the way you would read a well-written file, while it stays an outline you can rearrange in a second.

The same panel is where you and your agent meet:

- **Your agent reads the same document you see.** A slice of your Workflowy reaches the agent as exactly the Markdown the panel shows you, because both come from one renderer.
- **Your agent can show you things.** *"Render that in the modal"*: the agent sends a view to your panel, and you read it without the content ever passing through the agent's context.
- **Your agent can simply look.** Expand what matters, collapse what doesn't, and say *"GLIMPSE this."* The agent reads your current view, exactly as you have it open. That bare primitive was the first tool in the system, and it is still the whole on-ramp: no Docks, tags or slice groups required.

> 📸 **SCREENSHOT — "what you see, it sees":** side by side, two windows of equal width. Left: Workflowy zoomed into a small, non-personal subtree, e.g. **"⌨️ KEYBOARD SHORTCUTS & WORKFLOWY DESKTOP UI"** (`20cf213a`), with two children expanded and the rest collapsed (deliberately uneven). Right: a Claude Code terminal where the prompt was literally *"GLIMPSE this"*, showing the `mcp__workflowy__glimpse` call and the first ~25 lines of its Markdown result: the same headings, in the same order, with the collapsed branches absent. The resemblance between the two halves is the point; crop so both are legible at README width.

> 📸 **SCREENSHOT — "the agent captures, you read":** left: a Claude Code (or OpenCode) conversation in which the agent has just captured a note (`anchor` → `point.capture`) and reports the new handle, e.g. `[ap:Q3PL4N]`. Right: Workflowy showing that new bullet in its Dock, with the AP-ID at the front of its title, and the docked panel rendering it. Use a scratch Dock with an invented, neutral topic such as **"Q3 planning"**. Nothing else on screen.

> 🎬 **VIDEO (optional, about 60 s) — "watch this":** one take, no narration needed, captions only. (1) In Workflowy, click a beacon tag on a mapped function, or APPLY its slice; the modal renders the beacon slice. (2) In Claude Code beside it, ask a question the slice can answer, e.g. *"read this slice: which function composes the Markdown the modal shows?"* The agent answers from the slice, then makes **one** `beacon.snippet` call by function name. (3) Cut to the editor: the same `@beacon[...]` comment sits in the source file on disk. The reveal is that the map *is* the code. Choose a NEXUS tag with a short slice, e.g. `nexus--md-preview`. Link the video from here as a thumbnail.

---

## The mirror image

The tooling is fifty percent knowledge work and fifty percent source code, in both cases deeply integrated with Workflowy. Built on the tooling, the agentic harness has **the same two halves**:

| | **Knowledge work** | **Source code** |
|---|---|---|
| **The tooling (this repository)** | Anchor Docks and Anchor Points · slice groups · the docked Markdown modal | Cartographer maps · beacons and beacon slices · live code reads by symbol |
| **The harness built on it** | **Manager Sessions**: an agent that helps you think, plan, remember and curate | **The Program Conductor Toolkit**: agentic swarms that do the programming |

**It is a complete mirror image.** The harness combines planning a project, which is knowledge work, with running the project that writes the code, because to do effective source code you also have to be able to strategize the work. It is for architects, planners and founders, and for non-technical people who then have to do technical projects. *It's all of a piece.*

And all of it, the tooling and both halves of the harness, is **Workflowy-centric.** The plans, the memory, the code map, the swarm's instructions, every agent's assignment and every agent's report: one outline, one document.

> 📸 **SCREENSHOT — "both halves in one tree":** the docked panel rendering, headings-only, a selection that visibly holds both halves: a few knowledge-work Anchor Points (a Dock of invented, neutral notes such as **"Reading list"** and **"Q3 planning"**) **and** a mapped source folder from this repository (📄 file nodes with 🔱 function headings beneath them). If one view is awkward, two narrow crops side by side are fine. Create a small demo Dock for this if needed. Make sure no personal or work Docks appear.

---

## Why Workflowy: if it didn't exist, it would have to be invented

Workflowy is not just a data store here. It is a **graph-structured system with perfect visibility for people**, which works fundamentally well for both the knowledge-work side and the source-code side when agents work with people, combined with the fact that **it is one document**, all of a piece.

### You can't separate the data from the way humans see it

Start from the source-code idea alone: comments that agents keep beside the functions that matter, each tagging its function into a named topic. (Files form a graph; functions within files form a graph.) You could store the resulting map in SQLite. But then *there is no human visualization of the map*: a bare schema is unconstrained, becomes a database project, invites feature creep, and then demands a visualization tool built on top of it.

Once you had the system, you would want to see it. **The ideal visualization layer for that is Workflowy.** Filtering a map by tag is exactly what Workflowy tags already do. Workflowy is a visualization layer for its own data layer in the cloud, and it comes for free. If Workflowy didn't exist, you would have to write it from scratch, because that is what you want.

### One document: the constraint is the feature

A file system can store everything too, but it is too powerful to manage (which disk, which folder, several writers at once). One document is opinionated, and sometimes the constraints are as powerful as the flexibility.

### A folder you can read as one document

If you think of Workflowy's nodes as files (and they are), the outline is hierarchical like a file system, but you can see the files at the same time as you see where they are stored. I have used every Markdown tool I could get my hands on for years, and Obsidian is fantastic, but none of them let me click on what corresponds to a folder and simply **read every file inside it as one document.** Here that is the everyday gesture, and three more things come with it:

- **tags filter what that one document shows**, dynamically, without creating a new document;
- **order is yours**: move a bullet and the rendered order changes, where a file system sorts only by name or date;
- **restructuring is lightweight**: indent, outdent and reorder in a fraction of a second, with no file viewer anywhere near as convenient.

### Thousands of nodes is a sign of health

Anchor Docks are the containers, and slice groups cut across them; moving or adding is trivial, and there is no explosion of files. **Thousands of nodes in Workflowy is par for the course, and a sign of a good system; thousands of Markdown files is a sprawl.** Tagging files, in the sense that matters here (handing an agent a chosen, cross-cutting set of them, and seeing that set as one document), doesn't exist. If you set out to build it, you would end up building something like Workflowy. Why not just use Workflowy?

Like the NoSQL idea that a store's shape should match its use, **the ideal database for agent context management is the node-based database Workflowy already is**, and it already provides the perfect visualization layer. For fifteen years Workflowy has been a fantastic knowledge-management tool. It turns out to be something more: the right substrate for context management for agents. To build this system, if Workflowy didn't exist, it would have to be invented.

---

## The knowledge-work half

**Anchor Docks** are named containers: a project, a running thread, a shelf of reference pages. **Anchor Points** are what you capture into them, each a node whose note can be a whole Markdown document, carrying a permanent handle like `[ap:K7M2QF]` that any tool accepts wherever it asks for a node. That is how every agent in this system starts its job: its prompt says *"Read [ap:…] and follow it."*

- **Capture is locationless and tagless.** You say *"capture this"*; the agent picks the Dock, speaks that Dock's dialect (a to-do list gets to-dos, a log gets log entries) and files it. You never decide where a thing goes at capture time, and you never go back into Workflowy to file by hand. Curation comes later, and it is shared.
- **Slice groups are what you look at; Docks are where things go.** A slice group is a saved view (a root, a family of tags, a policy for expanding, suppressing or deprecating, a default sort) that renders as one document, by name. Lenses combine groups, tags and subtrees by union, intersection and difference without saving anything. A named **estate** bundles several of them into one reading list for an agent.
- **Recency is a first-class lens.** *"Everything in this slice from the last six hours"* or *"everything that arrived since I last curated"* is one call, sorted by when it was created or modified.
- **Search stays inside what you mean.** `sift` searches only within a slice group or lens, never the parts of the outline you have deliberately left out.
- **Finished work can leave without being lost.** Export a completed subtree to a verified archive file and delete it from Workflowy; it can still be read, searched and rendered, marked 📦.

A **Project Map** puts this together: one small page per project, listing that project's curated views, documentation and code-topic slices, so that every job on the project, by any agent, starts from the same well-chosen material. A tag added today puts a node into that context for tomorrow's agent.

```python
# Every call is family + verb + params; any MCP host can make these.
anchor(verb='point.capture', params={'parent_handle': '[ap:K7M2QF]', 'name': 'Supplier shortlist',
                                     'content': '## Three candidates\n…', 'ap_id': 'auto'})
glimpse(verb='run', params={'group_handle': 'Q3 planning', 'recent': '1w'})        # a saved view, last week only
slice_group(verb='sift', params={'lens': 'Q3 planning', 'query': 'budget OR "launch date"'})
glimpse(verb='run', params={'group_handle': 'Q3 planning', 'push_to_modal': True})  # show it to the human
```

### In the browser

The **docked Markdown modal** is the shared reading pane described above. Clicking a bullet re-roots it at that bullet. **🔒 Freeze** captures a view, so you can collapse and scroll Workflowy freely while you read. **Apply** renders a whole slice group from the server, complete, without any scrolling. A sort control reorders the document by created or modified time. A ⚡ Lightning Rod row makes throw-away selections by tag or text.

Beside it, the extension's **widget** shows every running background job, refreshes the local cache, finds any node from a pasted ID, lists your slice groups with an APPLY button each, and opens the **Anchors** popup: your favourite Docks, a table of contents for a life, each with a Render button. A handful of customizable hotkeys copy a node's path or ID (F2, F3, F4), expand everything under a tag prefix (F8), and open Cartographer's menu (Ctrl+F12). Everything runs locally: the extension reads the Workflowy page itself and talks to the server through a local, token-protected broker.

> 📸 **SCREENSHOT — the Anchors popup:** the widget's **Anchors** popup open over Workflowy, favourites-only, showing the ⭐ toggles and **[Render]** buttons, with one Dock just rendered into the modal behind it. ⚠️ Your real favourites include personal and work Docks: before capturing, temporarily star only NEXUS-related Docks (the NEXUS Project Map, a NEXUS documentation Dock, a demo Dock with a neutral name such as **"Q3 planning"**), or crop and blur. Crop to the popup plus a strip of the rendered modal.

> 🎞️ **GIF (optional, 10–15 s) — "the same outline, different lights":** in the widget's slice-group list, click APPLY on one group; the modal renders it. Then APPLY a second group over the same part of the outline; then a third. Use only groups with non-personal content (e.g. **CARTOGRAPHER DOCS** and two other NEXUS documentation groups) [TODO Dan: pick the groups]. This is the "dots of light" in motion. If a GIF is too much, a three-panel still strip works.

---

## The source-code half

**Cartographer** reads a repository on disk (every folder, file, class and function) and reflects it into Workflowy as a mapped subtree, one node per symbol, with each function's docstring and comments as its note. The mapping runs one way: **disk is truth.** The source never leaves the repository.

**Beacons** are small comment blocks that agents keep above the functions that matter:

```python
# @beacon[
#   id=myproj@load_rows,
#   slice_labels=myproj-ingest,
#   kind=ast,
#   comment=Reads one batch of rows and validates them before anything is written.,
# ]
def load_rows(path: str) -> list[dict]:
```

Each label becomes a Workflowy tag. A **beacon slice**, everything carrying one label, renders as signatures plus the comments their authors wrote: a table of contents with intent, assembled deterministically from disk with no model in the loop. A fifteen-thousand-line file becomes a few pages an agent reads in one pass, and then it knows exactly which function to open: `beacon.snippet` reads any mapped function **live from disk, by name**, with its real line numbers.

- **It doesn't drift, because it is the source.** The comments live in the code. Agents write and update them in the same edit as the code itself, and they do an incredibly good job of it: *curating, not carpet-bombing.* Documentation and code are much less prone to drift, and the next agent understands a complex codebase fast.
- **It serves architecture.** Deciding what the parameters and types should be, which new functions are needed and which components they belong on means seeing functions scattered across many files at once. That cross-cutting picture is exactly what a beacon slice is.
- **It is governed.** Mapping a large repository means many writes to Workflowy. An account-wide write governor, a breaker and a job panel in the widget keep that safe, built after a hard lesson in what too many writes can do to an account.

The **Agent Kit** (`agent-kit/`) takes the code half off Workflowy entirely: a standalone skill, `code-map`, that gives any repository beacon slices and live code reads from plain files, for any agent, with no server. *Give your agent a map, not grep.* It is built and its 626 tests pass; it has not yet been used by anyone but me, so call it en route.

> 📸 **SCREENSHOT — "a comment in the code, a map in the outline":** left: an editor showing one function in this repository with its `@beacon[...]` block directly above it (choose a function whose `comment=` reads well in plain English). Right: the docked modal showing that function's tag rendered as a beacon slice, headings-only: several 🔱 signatures across more than one file, each with its one-line comment. To produce it, ask an agent: *"render the beacon slice ‹tag› of the NEXUS map into the modal, headings only."* `nexus--md-preview` is a good candidate tag.

> 📸 **SCREENSHOT — the map and its jobs:** Workflowy showing this repository's mapped subtree, expanded folder → 📄 file → class → function, with one function node's note (its docstring) open; the widget's job panel beside it showing a recently **completed** map-refresh job with its per-file progress. No other branch visible.

---

## The tools are the skill

You don't need a skill file to use NEXUS. The tools explain themselves, in three layers: the top-level server description, the eight family descriptions that are always loaded, and progressive disclosure through `describe`. Each family lists its operations, each operation carries a full mini-manual, and any failed call returns the documentation for the verb it tried. You could just load the tooling; you don't even need skills.

| Family | What it does |
|---|---|
| `glimpse` | Render: the human's live view, the docked modal, a subtree, a slice group, an estate |
| `anchor` | Anchor Docks and Anchor Points: list, peek, render, capture, promote, search |
| `node` | Read, edit, search, create, move, complete and export nodes and their Markdown notes |
| `tag` | Add, remove, rename and batch tags; preview and apply clean-ups |
| `slice_group` | Saved views; lenses by union, intersection and difference; `sift` |
| `lightning_rod` | Temporary selections by tag, text or symbol |
| `beacon` | Read code by symbol, check and fix beacons, ensure tags, refresh the map |
| `system` | Health, cache, job status, configuration, export archives |

The whole surface costs about 11,000 tokens of always-on context, down from about 29,000 when it was forty-odd separate tools. For people who want more, a skill is coming in the form this system favours: a few lines that load a small, public Workflowy subtree as an agent's first instruction ([TODO Dan: public subtree link once published]).

---

## What it makes possible: a harness for agent swarms, in the same tree

Two large instruction systems run on these tools. Both are **Workflowy subtrees**, not documents on disk, and both are in daily use.

- **Manager Sessions** turns an agent into *the Manager*: the human's thinking partner and the operator of everything else. Every conversation gets its own Dock; what lasts is captured as Anchor Points and later promoted into topic Docks; curation decides what comes forward. Over 200 sessions so far.
- **The Program Conductor Toolkit** runs agentic swarms. A Conductor agent plans a program and writes a **socket** for each worker: an Anchor Point holding that worker's role, inputs, what it may change and its contract. It launches the workers, reads their results, runs independent audits and remediation, and hands off between its own sessions, all through Workflowy. As the Toolkit's own root page puts it, *"there is only one prompt and it never changes"*: `You are the CONDUCTOR. Read [ap:QE4RXD] and follow it.`

A whole program is visible as one outline: every assignment, every report, every audit, every account written for the human. The swarm's context management is the same dots of light: each worker's socket names exactly the nodes and slices it should see.

> 📸 **SCREENSHOT — a program in Workflowy:** the docked modal rendering, headings-only, the root of a finished Conductor program from the MCI work (open-source NEXUS work) [TODO Dan: MCI program root handle]: units, ⚖️ RESULT sockets, audits and their 🗣️ For the human children, the AP-IDs down the left. Check that no personal or work Docks appear in the frame.

These run on **OpenCode** and **Claude Code** through a separate repository, the **[Manager App](TODO-link)** [TODO Dan: final name and URL]: a desktop and phone cockpit for the Manager and every running agent, with the seat and helper scripts the swarms run on. **If you came here for agentic swarms, the Manager App README starts from that side.** It shows the swarms and the context management, then explains why the substrate is Workflowy. The two READMEs describe one system from two directions.

> 📸 **SCREENSHOT — cross-shot, the Manager App (desktop):** the Manager App with the **Programs** pane showing a running program's tree of Conductor and workers with live spinners, and the **View** pane rendering a Workflowy Dock exactly as the docked modal would. Use a program and a Dock with no personal content.

The coupling is honest and light. NEXUS stands on its own; the Manager App (whose View pane renders through this server) and the swarm instructions are built to use it.

---

## Status

| Component | Status today |
|---|---|
| NEXUS MCP server: daemon, shim, broker, eight families | **Working, used daily.** v1.0.1, classified Beta, MIT |
| Cartographer and beacons | **Working, used daily.** Map refreshes held in memory or in git worktrees are on the roadmap |
| GLIMPSE Chrome extension | **Working, used daily.** Load-unpacked for now; a UI redesign and a Chrome Web Store listing are planned |
| Agent Kit (`code-map`) | **En route.** Built; 626 tests pass; Python works with no dependencies, JS/TS needs tree-sitter |
| Skill file and public skill subtree | **Planned** |
| Manager Sessions and the Toolkit (Workflowy subtrees) | **Working, used daily.** Written for my own setup; curated public copies planned |
| Manager App | **Working, used daily.** Its own repository |

Each component's details, its dependencies and which README covers it are in **[COMPONENTS.md](./COMPONENTS.md)** [TODO Dan: final path].

What it is not, plainly. It is built around one person working with many agents; it is not (yet) a product for a team sharing one outline. It depends on a cloud service (Workflowy) and its API limits; export archives and Markdown export soften that, but this is not local files under git. And it has been used daily on one machine: expect rough edges on yours.

---

## Where this sits

The agentic world has converged on curated Markdown, progressive disclosure, compaction and subagents. NEXUS leans hard on the first two, and in place of compaction it uses fresh agents with explicit handoffs through Workflowy: handoffs are the reliable path, summarization is not. To be fair about what is and isn't new here:

- **Agents reading and writing an outliner is not new.** Workflowy now ships [its own MCP server](https://workflowy.com/help/mcp-server/), and [Tana](https://tana.inc/docs/local-api-mcp) has one too. What is new is what is built on that access: permanent handles, curated slices rendered as documents, the shared reading pane, code mapped into the same canvas, and a harness for swarms on top.
- **Shared agent memory as Markdown is not new.** [Letta's context repositories](https://www.letta.com/blog/context-repositories) keep it in git, which gives them history and branches that Workflowy doesn't have. What NEXUS adds is a visible, human-curated selection of exactly which memory each agent sees.
- **Tagged notes are not new.** Obsidian has tags and query views. What NEXUS adds is a tag selection that *is* the agent's context, rendered as one live document you read beside the outline.
- **No numbers yet.** Nobody, including me, has published measurements showing that tag-selected context beats well-curated files. The claim here is the design, and the fact that it carries real work every day.

---

## Trying it

> ⚠️ This repository is a portfolio piece first. Installation works, but it hasn't been made easy yet; that comes next.

You need:

- a Workflowy account and its API key (`WORKFLOWY_API_KEY`, the one credential);
- Python 3.10+ and [uv](https://docs.astral.sh/uv/);
- an MCP host (Claude Code, OpenCode, Claude Desktop…);
- Chrome 111+ for the extension.

1. **Point your MCP host at the server.** It is a stdio command, `workflowy-mcp`, which starts one background daemon on `127.0.0.1:8766` on first call. HTTP-native hosts can connect to `http://127.0.0.1:8766/mcp` directly, with the bearer token.
   ```bash
   uvx --from git+https://github.com/daniel347x/workflowy-nexus-mcp-tools.git workflowy-mcp
   ```
2. **Load the extension:** `chrome://extensions` → Developer mode → *Load unpacked* → `workflowy-glimpse-cache/`. Paste the broker token into the extension's options page (from `WORKFLOWY_BROKER_TOKEN`, or the token file the server generates).
3. **Open workflowy.com, expand something, and ask your agent to GLIMPSE it.**

The server is designed to run natively on Windows, macOS or Linux; it refuses to run inside WSL, where clients connect to the daemon on the Windows side. It is developed and used daily on Windows [TODO Dan: confirm whether macOS and Linux have been exercised]. Configuration is in [`CONFIG_REFERENCE.md`](../../CONFIG_REFERENCE.md) [TODO Dan: path from the final README location].

---

## Who made this

This is not a company. It is one person.

I'm Dan Nissenbaum. I spent five years as a principal at Microsoft, as an individual contributor. I've never been that active in open source, but incredibly active in private source. I'm not a good marketer, either. I've been holed up, working non-stop with these agentic systems and finding ways to make them work the best, and this is what I've landed on. I've got something I'd like to share now.

The question a careful reader asks of a project like this: did other people build it, or does it wrap somebody else's framework? **A hundred percent no, across the board.** Workflowy and its API are Workflowy's. The one other starting point was a minimal example Workflowy MCP wrapper, [`vladzima/workflowy-mcp`](https://github.com/vladzima/workflowy-mcp) [CONFIRM Dan: vladzima/workflowy-mcp] (MIT), which made the most basic use of the API: a huge starting point, and about one percent of what is here. Its package and command names, `workflowy_mcp` and `workflowy-mcp`, still survive in this code.

And I wrote almost none of the code. Agents did, working inside this system as it grew, while I drove every idea and insisted on good design. That's not a confession; it's the point. **This is a fully agentic supporting system,** built with itself.

[TODO Dan: contact line: email, LinkedIn or site]

---

## License

MIT. See [LICENSE](../../LICENSE) [TODO Dan: path from the final README location; and, if the upstream credit above is confirmed, carry the original MIT notice alongside your own].
