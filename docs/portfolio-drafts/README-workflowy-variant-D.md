# NEXUS · Workflowy as the shared memory of a human and their agents

> **What you see, your agent sees.** Zoom into a bullet in Workflowy. Expand what matters, collapse what doesn't, and say *"GLIMPSE this."* The agent reads exactly that view, your zoom and your expansions, as one clean Markdown document. It runs the other way with the same ease: the agent renders what it assembled into a panel docked beside your outline, and you read it without spending a token.
>
> *Human: you never describe context; you point at it. Agent: you never guess context; you look.*

NEXUS is an MCP server, a code mapper and a Chrome extension that turn one Workflowy outline into the shared, visible memory of a person and the agents working for them. Half of it supports **knowledge work**: capturing, remembering, planning, curating. Half of it supports **source code**: a live map of a repository, kept current by the agents that work in it. Both halves run on the same three primitives, and all of it is Workflowy-centric.

Built on top of it, and open-sourced with it, is an agentic harness with the same two halves. **Manager Sessions** turn conversations with an agent into a record the next session picks up without you re-explaining anything. The **Program Conductor Toolkit** runs swarms of agents that write code, dispatched, audited and reported through the same tree. The **Manager App**, in its own repository, is the cockpit for all of it on desktop and phone.

*One person built this, with agents, using the system it describes. More on that at the end.*

> 📸 **SCREENSHOT — hero, "what you see, your agent sees":** One browser window on workflowy.com with a neutral project subtree expanded on the left (something like "Kitchen renovation" with a dozen bullets and a couple of notes) and the docked Markdown modal open on the right showing that same subtree rendered as one document. Beside or below it, a terminal with a Claude Code or OpenCode session showing a `glimpse(verb='run')` call and the first fifteen lines of the Markdown it received. Full width; the three surfaces must be readable at a glance.

---

## Two readers, one page

**If you have never used Workflowy:** read this as an answer to a question you already have. How do you give agents the right context, not too much and not too little, keep it current, and see what they saw? The answer here is a graph of nodes in one document, with tags that cut across it, and a rendering layer both of you read. We chose Workflowy because it already is that graph, with a visualization layer that comes for free. The rest of this page is what you can build on it once agents can read and write it properly: memory you can see, a code map that never drifts, and swarms whose paperwork is a page you can open.

**If you live in Workflowy:** you have probably sensed that your outline should be part of your agentic work, and found no framework that does it. This is that framework. Your agents capture into your Docks, cite your bullets by permanent handle, render any slice of your tree as a document, and read your source code through the same outline. Nothing is copied out; nothing is pasted in. Your Workflowy becomes fully agent-enabled, on both sides of your work: the life you manage and the code you ship.

The two readers meet in one sentence from the person who built this: *"If Workflowy didn't exist, it would have to be invented."*

---

## The problem this is built for

Context management is the defining problem of the agentic age. In the words of the system's author:

> *"Context management, prompt engineering, memory, agents communicating the relevant material, not too much, not too little, is obviously foundationally the challenge in effectively using agents. It's the underlying driving force behind agentic harnesses."*

The industry has converged on an answer: curate your Markdown files. Instruction files at the project root, rules folders, skills that load on demand, memory folders with an index. It works, up to a point, and then it sprawls. The files grow and nobody trims them. They go stale. Two of them contradict each other. Nothing on disk says which ten of your two hundred notes this particular agent should see today, and there is no good way to tag a file so that it joins a set.

Auto-summarization and compaction are the fallback, and they are *"not really that great... not the real solution."* The real solution is curation. The question is what substrate makes curation cheap enough that a person actually does it.

> *"The ideal solution to that is a node-based, tag-based system, which is exactly what Workflowy represents... because nodes in that system are units of memory, and it's only a certain subset of those that are relevant at any given time."*

Every Anchor Point in this system is Markdown, rendered as Markdown, exportable to disk. But it is also a node: it has a place in a tree, it carries tags, it can be moved with a drag, and a tag added to it puts it in a slice that renders later as an agent's context. The image the author uses for what that looks like at scale:

> *"If the nodes were out in outer space, and a bunch of them lit up for one agent, and then another set of them lit up for another agent... You just see all these little dots of light, lighting up and vanishing. That is context management."*

Thousands of nodes in Workflowy are *"par for the course. That's a sign of a good system."* Thousands of Markdown files are a sprawl.

---

## Why an outline, and why this one

The substrate argument, distilled from the author's own account of years spent trying every Markdown and note tool he could get his hands on.

**One document.** Workflowy can store everything, but it is very different from a file system. A file system is too powerful to manage: which disk, which folder, multiple writers at once. One document is opinionated, and *"sometimes the constraints are as powerful as the flexibility."*

**A folder you can read as one document.** A subtree is a folder with subfolders. Click it, and the docked panel renders every file inside it as one continuous document. Tags filter what that document shows, dynamically, without creating a new document. Nodes represent documents without the heavyweight aspect of being documents; export to files or import from them whenever you need to.

**Order and restructuring are free.** Moving a bullet changes the render order; a file system sorts by name or date. Indent, outdent and reorder take a fraction of a second. If you think of nodes as files, and they are, you get a hierarchy like a file system in which you can see the files and where they are stored at the same time.

**You can't separate the database from how humans view it.** The code-mapping half of this system could have been built on SQLite. The downside: no human visualization of the maps. A bare schema *"opens up too many possibilities. It's unconstrained. It just becomes a database project,"* and then it demands a visualization tool built on top of it. Start from the beacon idea alone, disk-backed comments maintained by agents, and ask what you would want to see them in. *"The ideal visualization layer for that is Workflowy. Like, that's it. If Workflowy didn't exist, you'd have to write it from scratch."* Workflowy is a visualization layer for its own data layer, and it comes for free.

That is the whole bet. Not "a Workflowy integration," but the claim that a node-based outliner with tags and a free rendering layer is the right substrate for agentic knowledge work, source-code work and swarm harnesses, and that Workflowy is the one that already exists.

---

## The mirror image

The tooling is *"fifty percent knowledge work supporting and fifty percent source code supporting, in both cases deeply integrating with Workflowy."* The harness built on it has the same two halves. *"Because in order to do effective source code, you have to also be able to strategize the work."*

```
                 KNOWLEDGE WORK                      SOURCE CODE
   tooling       Anchor Docks · Anchor Points        Cartographer · beacons
   (NEXUS)       slice groups · GLIMPSE · the modal  beacon slices · snippets
   harness       Manager Sessions                    Program Conductor Toolkit
   (on top)      think · plan · remember · curate    swarms that write the code
                 ───────────── one Workflowy tree · one Markdown view ─────────────
```

*"So you can see it's a complete mirror image."* This is for architects, planners, founders, and people who are not primarily programmers but have technical projects to run: the planning is knowledge work, the running is code, and they live in the same tree. *"It's all of a piece. We've got it all."*

The sections below walk the diagram: the knowledge half, the panel both of you read, the code half, then the harness.

---

## Knowledge work: Docks, Points and slices

Three primitives carry the knowledge half. They are ordinary Workflowy bullets with a little discipline added.

**Anchor Docks are where things go.** A Dock is a named container node, discoverable by the agent (`anchor(verb='dock.list')` returns your starred shortlist). Docks nest: a parent Dock shows each sub-project as one line, because a Dock render stops at child Docks. Docks are untyped on purpose; a to-do Dock gets to-do-shaped items, a decisions Dock gets decisions, and the agent infers the dialect from the Dock's title and description. A Dock doubles as an ad-hoc to-do list: peek its most recently touched items and you see what has your attention.

**Anchor Points are what you capture.** Any node can be one. Give it an AP-ID and it carries a permanent citation handle at the front of its name, `[ap:K7M2QF]`, that survives renames, moves, sorting and completion. Every tool accepts that handle wherever it asks for a node, so prompts, results and documents cite memory by handle, and the handle keeps resolving after the tree is reorganized. Capture is the `write` of the graph: the agent passes Markdown straight in, and one rich note beats a pile of bullets.

```
anchor(verb='point.capture', params={
  'parent_handle': '[ap:KTCHN1]',          # the "Kitchen renovation" Dock
  'name': 'Contractor quotes, compared',
  'content': '## Three quotes\n...',       # the whole Markdown document
  'ap_id': 'auto'})
```

**Slice groups are what you look at.** A slice group is a saved view: a root, a tag family, a policy for what expands and what is hidden, and a default sort. Tag a node `#project-launch` and it joins the "Q3 planning" slice; render the slice by name and every member, wherever it lives in the tree, comes back as one document. A `--expand` postfix on a tag pulls a whole subtree in; `--suppress` hides one always. Lenses combine slices, Docks and subtrees by union, intersection and difference without saving anything. The cheapest reads are headings-only: titles, handles and sizes, so the agent knows how big a thing is before reading it.

```
glimpse(verb='run', params={'group_handle': 'Q3 planning'})                     # the whole slice, one document
glimpse(verb='run', params={'group_handle': 'Q3 planning', 'recent': '6h',
                            'sort_by': 'created'})                              # "everything we did in the last six hours"
slice_group(verb='sift', params={'lens': 'Q3 planning', 'query': 'budget'})    # grep, but only inside the slice
```

**Capture is locationless and tagless.** You never decide where a thing lives at capture time, and you never hand-pick a raw tag. The agent routes the capture to the right Dock; the saved view dissolves the tags; a chronological render dissolves the location. Curation is a later pass: a two-pass pattern of headings-only read, pick the keepers, tag them into a view. And when a subtree is finished, it can be exported to a verified archive file, deleted from Workflowy, and still read, searched and rendered, marked 📦. Thousands of nodes, no sprawl.

> 📸 **SCREENSHOT — a capture landing:** Left, an agent transcript (Claude Code or OpenCode) with an `anchor(verb='point.capture', ...)` call and its acknowledgement showing the allocated `[ap:…]` handle. Right, Workflowy with the new bullet visible inside its Dock, the handle at the front of its name and the first lines of its note expanded. Use a neutral Dock such as "Kitchen renovation". Crop to the two panes.

> 📸 **SCREENSHOT — a slice group applied:** The extension's widget with the slice-group list visible (neutral group names only, e.g. "Q3 planning") and its green APPLY button, with the docked modal beside it showing the rendered slice: three or four sections whose headings clearly come from different parts of the tree. Crop to widget plus modal.

---

## The panel both of you read

Before any keyboard shortcut is named, the one surface worth understanding: the **docked Markdown modal**.

It is a full-height panel docked to the right edge of Workflowy, opened from the extension's widget. It renders whatever is visible as one clean, scrollable Markdown document: any subtree, spanning any number of nodes, with heading levels managed so the result reads as one piece. Click a bullet and that bullet becomes the top of the document; click another and the document follows. The top half of the panel is a flat list of the nodes in the render; the bottom half is the rendered Markdown for the row you select.

Two things make it more than a preview:

- **The agent can write to it.** Any server-side render can be pushed into the panel (`push_to_modal: true`) instead of into the conversation. *"Show me this Dock, in full"* costs the agent zero tokens: the human reads the panel. And the agent can read the panel back, exactly as it stands, with `glimpse(verb='run', params={'render_spec': 'modal'})`, whatever produced it.
- **It can be frozen and applied.** Freeze snapshots the current document so you can collapse and rearrange your outline while still reading it. Apply renders a whole slice group server-side into the panel, no scrolling and no expanding by hand, as a stable document you can curate against: retag, deprecate, refresh, and watch the slice update while the panel stays put.

Then the gestures, which you will learn in an afternoon: F3 copies a bullet's path for pasting into a prompt, F8 expands every subtree tagged for a slice, and Ctrl+F12 opens the Cartographer menu on a mapped file. The widget also shows every running job, lets you paste a UUID the agent mentioned and jump to it, holds an Anchors popup that lists your Docks with a Render button beside each, and lets you alias a tab so an agent can be pointed at *that* tab when several are open.

The extension reads the page in the browser; it is not built on the API. It talks to the server through a local broker with a shared secret. The extension's user interface is due for a redesign; what is described here is what runs today.

> 📸 **SCREENSHOT — the docked modal in detail:** Workflowy with the modal docked, showing its ribbon (Freeze, Refresh, the sort control and the Rendered/Source toggle), the flat node list in the top half with one row selected, and that row's rendered Markdown in the bottom half, composed from at least two sibling subtrees so the composition is visible. Neutral content. Crop to the modal and a sliver of the outline.

> 📸 **SCREENSHOT — the Anchors popup:** The widget's Anchors popup open in favorites-only mode, four to six Docks listed with their paths and the Render button on each row, one Dock starred. Only neutral Dock names may be visible; scroll or choose a view where nothing personal or work-related shows. Tight crop.

---

## Source code: Cartographer, beacons, slices and snippets

This half needs saying precisely, the first time.

**Cartographer** reads a repository on disk, every folder, file, class and function, and reflects it into Workflowy as a mapped subtree: one node per symbol, each function's docstring and adjacent comments as the node's note. The mapping is one way, **disk to Workflowy, and disk is truth**. The source of truth never leaves the repository; the map is a projection, refreshed from disk by one governed job.

**A beacon** is a comment block you write above a function that matters. Its labels become Workflowy tags on the mapped node, so the code is sliced exactly the way notes are. Python, JavaScript and TypeScript, Vue, SQL, shell and Markdown all take the same block behind their own comment prefix:

```python
# @beacon[
#   id=shop@list_orders,
#   slice_labels=shop-api,
#   kind=ast,
#   comment=GET /orders: paginated order list for one customer; the entry point the checkout page calls.,
# ]
@router.get("/orders")
async def list_orders(customer_id: str, page: int = 1) -> list[Order]:
    ...
```

**A beacon slice** is the code selected by a label, rendered as signatures plus the comments beside them. No bodies. A fifteen-thousand-line file becomes a few pages an agent reads in one pass, including the functions in files it would never have thought to search, and then it knows exactly which symbol to open next. Assembly is deterministic and costs no model tokens: the slice is parsed from disk, not composed by a model. Because it *is* the source, it never drifts.

**A snippet** is the whole function by name, live from disk, numbered by file line:

```
beacon(verb='snippet', params={'source_handle': 'list_orders'})
```

Every function and class in a mapped file resolves by name, beaconed or not, so an agent follows a call chain the way a person does: read the slice, open one body, follow the name it calls. Past sixty-four lines a snippet shows its head and tail and names the window that reads the rest. Compare this with a recursive grep that finds one line at a time and costs a round trip per hit; the agents in this system are told, in so many words, *"when you think file search, think Slice Catalog."* A Slice Catalog is a page listing a project's beacon tags with a sentence each: a menu of slices, kept on the project's own map.

**The map maintains itself, because agents maintain it.** An agent that adds an entry-point function writes its beacon in the same edit, with a comment that says what the function does. Agents turn out to be *"incredibly good"* at curating these rather than carpet-bombing them, which is why documentation and code are much less prone to drift here, and why a new agent understands a complex codebase quickly. The beacon tools write the disk only; the map catches up at the next refresh, which the extension and the tools both expose, and which is never looped: a refresh is a bulk account-wide write, paced by a governor that exists because an early version learned that lesson the hard way.

> 📸 **SCREENSHOT — a mapped repository in Workflowy:** This repository's own map: a folder node expanded to a file node expanded to its class and function nodes, two or three of them carrying beacon tags, one function node expanded to show its docstring as the note. Crop so the folder/file/function nesting and the tags are legible.

> 📸 **SCREENSHOT — a beacon slice rendered:** The docked modal showing one beacon slice from this repository rendered headings-only: file paths as headings, signatures beneath with the 🔱 marker on beaconed symbols and each beacon comment beside its signature, ten to twenty symbols from at least three files. Crop to the modal.

> 📸 **SCREENSHOT — a snippet in a transcript:** An agent transcript showing a `beacon(verb='snippet', ...)` call by symbol name and its result: the header line with file, line range and size, then the numbered source lines. Crop to the call and the first twenty lines of the result.

> 📸 **SCREENSHOT — a map refresh in the widget:** The extension's widget while a Cartographer refresh runs: the job row with its status, the per-file progress, and the Refresh-cache button. Small crop; the point is that the human watches governed jobs happen.

---

## On top: the harness

Everything above is tooling. What it makes possible is the reason it exists.

**Manager Sessions** are the knowledge-work half of the harness: a set of instructions, written as a Workflowy subtree, that turn an agent into your standing thinking partner. Every conversation gets its own Dock. Whatever is worth keeping is captured as an Anchor Point while you talk; later, a curation pass promotes the durable items into their topic Docks, each with a trail line saying where it came from. The next session starts by reading what is current, not by you reconstructing it. The instructions are the skill, and *"the skill is a Workflowy subtree. It's not a document on paper."* The author has run more than two hundred of these sessions; the system you are reading about was managed through them.

**The Program Conductor Toolkit** is the source-code half: doctrine, recipes and worker clauses, also a Workflowy subtree, for running agentic swarms. A Conductor agent plans a program and writes a **socket** for each worker: an Anchor Point holding a dispatch block that says who the worker is, what it reads, what it may write, and what it is measured against. Workers read their instructions by handle, read code through beacon slices and snippets, and fill their sockets with results. Auditors check the work against a stated bar; remediation rounds fix what they find; every report has a plain-language *For the human* child so the person steering can tell a threatened design from a routine fix. The Conductor's own prompt is one line and never changes: *read this Anchor Point and follow it.* Programs run on OpenCode and on Claude Code; this README was drafted by an agent dispatched through one of them.

**The Manager App** is the cockpit, in its own repository: dictate to the Manager, refine the dictation with a frontier model that has read the conversation, send it; read the Manager's conversation live; see every program, Conductor and worker as a tree with live status and context telemetry; and open any Workflowy document in a View pane rendered exactly as the docked modal would render it. Desktop and phone. It is coupled to Workflowy by design and lightly in code: its View pane is an MCP client of this server. Read its README for the swarm side of the story. [TODO Dan: link to the Manager App repository and README]

The two subtrees are referenced from both READMEs. Public, read-only copies are planned, with Markdown exports committed beside them so that any agent can read them from the repository. [TODO Dan: links to the public Manager Sessions and Toolkit subtrees once shared]

> 📸 **SCREENSHOT — the Manager App's programs pane (cross-screenshot):** The desktop Manager App with the programs pane showing one running program: the Conductor at the top, three or four workers beneath with live status and context percentages, spinners active. Use this open-source program or any program with neutral names. Crop to the pane.

> 📸 **SCREENSHOT — a socket and its result:** Workflowy showing one worker socket: the dispatch block at the top of the note (role, inputs, writes), the `## Result` beneath it, and its `🗣️ For the human` child node. Pick a socket from this open-source program so nothing private appears. Crop to the socket.

> 🎬 **VIDEO (optional, 60 seconds):** Click a beacon tag in Workflowy; the slice renders in the modal; an agent is asked to make a small change and reads one function by snippet; the change lands with its beacon updated in the same edit; cut to the source file on disk to show the beacon block is just a comment. No narration needed beyond captions.

---

## A fair comparison

Claims here are qualitative on purpose, and the neighbours deserve naming.

- **Instruction files and skills** (CLAUDE.md, AGENTS.md, rules folders, SKILL.md) are the industry's curated-context standard, and NEXUS does not replace them. They cover constraints and workflows; beacon slices cover code orientation; Anchor Points cover memory. The difference is that selection here is by a human-chosen set of nodes rendered as one document, rather than by file path, folder position, or the model guessing from a description.
- **Agent memory products** (the memory features in Claude, ChatGPT and Gemini, and memory servers over vector or graph stores) select by the agent's own query and store what the agent chose. Here the memory is a bullet you can read, move and edit, and you choose what an agent sees.
- **Outliners with MCP.** Workflowy ships its own desktop MCP server, and Tana has an official one over its nodes and supertags. Access is not what is new here; the system on top of it is: Anchor Points with permanent handles, slice groups and lenses, the docked panel, Cartographer, and the harness.
- **Markdown knowledge bases.** Obsidian is fantastic, and its query views can collect notes into one view. What NEXUS adds is the combination: a folder that reads as one document, tags that select agent context across the whole tree, order you set by moving a bullet, and code mapped into the same canvas.
- **Shared Markdown memory for agents** (git-backed context repositories) is the closest in spirit on the swarm side, and it brings commit history, which Workflowy does not. NEXUS brings the human-visible outline and the code map.
- **Retrieval over code.** A vector index retrieves by similarity to the query; a beacon slice retrieves by location and by what a human or agent wrote at the function. They complement each other.

Nobody, the author included, has published a controlled measurement that node-and-tag selection beats curated files. What this repository offers instead is a system in daily use at scale, with its own development run through it, and the reasoning above.

---

## The tools, in one screen

The server exposes **eight tools**, one per family, with seventy-four verbs between them. Every call is `family(verb=..., params={...})`, and handles are uniform: a node is addressed by its UUID, the first eight hex characters of its UUID, or its `[ap:…]` handle, in every family.

| Family | What it is for |
|---|---|
| `glimpse` | Render: the live view, the docked panel, a known root, a slice group, or an estate (several views as one document) |
| `anchor` | Anchor Docks and Anchor Points: list, peek, render, create, capture, promote with provenance |
| `node` | A node's note as a Markdown document: read with line windows, grep, edit with search-and-replace, append, etch a forest, move, complete |
| `tag` | Tags through parameters, never typed into names; all-or-none batches |
| `slice_group` | Saved views, lenses by union, intersection and difference, and `sift`: search only inside a lens |
| `lightning_rod` | Disposable selections: by tag, by smart-text or regex search, by symbol list |
| `beacon` | Mapped source: snippets by name, beacon labels on disk, checks and fixes, the governed map refresh |
| `system` | Health, the broker, the cache, asynchronous jobs, export archives |

**The tool descriptions are the skill.** A server description, eight always-on family descriptions, and `describe` for any family or verb give an agent everything it needs through progressive disclosure; a failed call returns the verb's own documentation. You could just load the tooling. You don't need a skill file. Those descriptions are long by current fashion, on purpose: coherence is what an agent needs, and coherence takes words. What used to be more than forty separate tools is now eight.

Lighter forms are on the way, for whoever wants them: a skill file, agents files for Claude Code and OpenCode, and a small public Workflowy subtree that plays the role of a skill (the whole skill file being a few lines that load it). A packaged "Workflowy agent" exists in pieces, not yet as a package. [TODO Dan: confirm which of these ship with the first public release]

---

## Try it

You need a Workflowy account with an API key, Python 3.10 or newer with `uv`, and an MCP host (Claude Code, OpenCode, Claude Desktop, or any client that speaks stdio or streamable HTTP).

```
# the server, straight from the repository
export WORKFLOWY_API_KEY=...
uvx --from git+https://github.com/daniel347x/workflowy-nexus-mcp-tools.git workflowy-mcp
```

Point your MCP host at the `workflowy-mcp` command (stdio; it starts the daemon on `127.0.0.1:8766` on first use) or at `http://127.0.0.1:8766/mcp` with the bearer token. The daemon runs natively on Windows, macOS and Linux; under WSL, clients dial the Windows daemon.

For the browser side, load `workflowy-glimpse-cache/` unpacked in Chrome, paste the broker token into the extension's options page, open workflowy.com, and press F3 on a bullet. The server starts the local broker for you.

Then say *"GLIMPSE this"* to your agent. The bare core needs nothing else: no Docks, no slices, no setup beyond this. Everything above is waiting when you want it.

[TODO Dan: verify these steps on a fresh machine before publishing; the extension's install docs still name an older version, and Chrome Web Store publication is the next milestone.]

---

## What is in this repository

A short orientation; the full component list with status lives in [COMPONENTS.md](COMPONENTS.md).

| Component | Status today |
|---|---|
| **NEXUS MCP server** (daemon, stdio shim, broker, the eight families) | Working, used daily. MIT. |
| **Cartographer and beacons** | Working, used daily. In-memory and worktree map refreshes are planned. |
| **GLIMPSE Chrome extension** (the modal, the widget, the gestures) | Working, used daily. Load-unpacked; a UI redesign is coming. |
| **The tool descriptions** (the built-in skill) | Working; the settled wording. |
| **Agent Kit** (`agent-kit/`: a `code-map` skill giving any repository beacon slices and live reads from disk alone, no Workflowy) | En route: built and tested on Python repositories with the standard library only; JavaScript and TypeScript need tree-sitter installed. |
| **Manager Sessions** and the **Program Conductor Toolkit** | Workflowy subtrees, used daily; public copies planned. |
| **Manager App** | Working, used daily; its own repository. |
| **Skill file and public subtree** | Planned. |

Size, for the reader who wants to know what they are looking at: about 168,000 lines of first-party code across the server, the mapper and the extension, 5,400 test functions, and about 1,300 commits since January 2026.

---

## Honest limits

- Workflowy is a cloud service and a proprietary dependency. The system reads your account through its API key and nothing else; it holds no copy of your data except a local cache it refreshes.
- Every mutation is verified against live Workflowy before and after it is written; *the system does not trust its own memory about anything it is about to change.* The one bounded race is editing the very node an agent is writing at the same instant. Batch heavy manual editing, then refresh the cache once; agent-driven work needs no refresh.
- A map refresh is a bulk write across your account, paced by a write governor and a breaker. Run one, whole-repository, and poll it; never loop it.
- The extension is installed by loading it unpacked. The server is not on PyPI yet.
- This was built for one person's machines, and the documentation shows it. A stranger can install it, and the server contains no machine-specific paths, but nobody outside the project has done so yet. Expect rough edges and say so.

---

## Who made this

Not a company. One person: Daniel Nissenbaum, an ex-principal at Microsoft for five years, an individual contributor, *"never that active in open source, incredibly active in private source."* In his words: *"Not a good marketer, but I've got something that I'd like to share now."* And on how it came to be: *"Just holed up, non-stop working with these agentic systems and finding ways to make them work the best, and this is what I've landed on."*

Agents wrote the code. That is an advertising point, not a confession: *"This is a fully agentic supporting system."* One human drove every idea, insisted on good design, and kept the project moving, using the system this page describes to build the system this page describes, through the Manager Sessions and the conductor programs above. Version one is locked and sealed; the improvement waves continue.

Nothing here wraps an existing framework. The one starting point not his own was a minimal example Workflowy MCP wrapper, *"one percent of what I have."* [CONFIRM Dan: vladzima/workflowy-mcp, MIT] Workflowy itself and its API are, of course, Workflowy's.

One immodest sentence, then: I have not seen another system in which a person's plans, their memory, their code map and the paperwork of their agent swarms are one document that they actually read every day.

If that is the kind of work you are looking for, the code, the tests and the history are all here. Look around.

---

*MIT License. Built with Workflowy, not by Workflowy.*
