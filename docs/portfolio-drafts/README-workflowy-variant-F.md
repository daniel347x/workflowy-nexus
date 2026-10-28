# NEXUS

## Your agent sees what you see. Your work survives the conversation.

Open [Workflowy](https://workflowy.com/). Expand the branches that matter. Say, **“Look at this.”**

Your agent reads that view as Markdown. It can discuss it with you, capture a decision beside the evidence, and leave a record the next session can pick up. You are not assembling a prompt from scattered files or asking the agent to reconstruct what happened. You are pointing at the work.

**NEXUS is a Workflowy-native context-management system for agents and humans working together.** It combines an MCP server, a Chrome extension, and a code-mapping system called Cartographer. Plans, research, decisions and source-code maps share one canvas. Human and agent can read the same selected material as one document.

Half knowledge work. Half source code. Built on top: a matching pair of agentic harnesses—**Manager Sessions** for thinking, planning and memory; the **Program Conductor Toolkit** for running swarms that do the work.

If you already use Workflowy, this makes it a workspace for your agents. If you came for agentic development, this is why you might want Workflowy in the first place.

> 📸 **SCREENSHOT — One record, two views:** In today's Chrome extension, open a neutral `Project launch` Dock with three note-bearing children: `Research`, `Decision`, and `Next steps`. Use the widget's **Anchors → Render** action to show that Dock in the right-hand Markdown panel. Select the Dock's root row so the bottom pane shows all three notes as one document. Capture the outline on the left and the panel on the right, with its compact node list above the rendered prose. Keep the headings and one short decision readable; crop out unrelated branches and browser chrome.

*The outline gives the work its structure. The reading panel turns it into a document. The agent can read that same view.*

## Context is a selection, not a transcript

A long conversation is not the same thing as a useful memory. The question for the next agent is not “What did we say?” but “What matters for this job?”

NEXUS makes that question visible.

Each Workflowy node can hold a full Markdown document. A node might be a decision, a design, a piece of research, or an instruction. It can have a permanent citation handle—an **Anchor Point ID**—that stays with it when it moves or gets renamed. **Anchor Docks** are the named containers where this material lives.

Then **slice groups** select across those containers. A saved `Q3 planning` view can bring together the relevant research, decisions and open questions without copying them into a new document. Add a node to the selection and it joins the next render. Change the note and the next render includes the change. The material stays in place; what you look at changes.

Now give different selections to different agents:

- A planning session gets the research and constraints.
- An implementer gets the approved design and the relevant code.
- An auditor gets the contract and the work to examine.

Imagine the nodes as points of light: one set lights up for one agent, another set for the next. **That is context management.** Here, you can see the lights.

## Two halves, neither an afterthought

### Think, plan, remember

“Capture this decision.” “Bring me up to date on the launch.” “Show me the open questions.”

The agent can discover your Docks, read their descriptions, and put a new Anchor Point where it belongs. During a working session, capture the useful material as it emerges; later, curate the durable pieces into their topic Docks. NEXUS can move those points with a trail back to the session they came from, or make a derivative copy while leaving the original intact.

**Docks are where things go. Slice groups are what you look at.** You do not have to reorganize the whole outline to prepare for a different conversation.

There are several ways to choose a view:

- **Point at it:** let the agent read the Workflowy view you have open.
- **Name it:** render a saved slice group, a Dock, or a particular node.
- **Compose it:** combine selections with union, intersection or difference; search within the selected material rather than the entire history.
- **Read what changed:** show recent arrivals or recently worked-on items, in document or chronological order.

The Chrome extension adds a reading panel beside the outline—the **docked Markdown modal**. It composes the selected notes into one continuous document, rather than making you open them one by one. The agent can send a view into that panel; you can inspect it, and the agent can read back what the panel shows.

This is memory you can read, rearrange and work on together. The useful outcome of a conversation becomes part of the record, not something trapped inside that conversation.

### Understand and change source code

Architecture rarely fits inside one file. The functions that explain a feature may be scattered across a parser, a server, an extension and a test harness. An agent needs that picture before it starts changing things.

**Cartographer maps a repository into Workflowy:** folders, files, classes and functions become a navigable outline. **Beacons** are small comment blocks beside selected symbols in the source. Their labels become tags on the map; their comments explain why those symbols matter.

For example, an entry point in a release tool could carry:

```python
# @beacon[
#   id=release@build_manifest,
#   slice_labels=release-pipeline,
#   kind=ast,
#   comment=Builds the release manifest from the validated file list.,
# ]
def build_manifest(paths):
    ...
```

A **beacon slice** gathers the symbols for a topic across files, showing their signatures and beacon comments. It gives the agent a map of intent before it reads function bodies. Assembly is deterministic: the slice comes from the mapped source, not a model-generated account of what the code probably does.

When the agent needs the implementation, it asks for the symbol:

```python
beacon(verb="snippet", params={"source_handle": "build_manifest"})
```

That reads the source live from disk, with file line numbers. A mapped function does not need its own beacon merely to be read; beacons curate the entry points worth gathering into a slice.

**Disk is truth.** The repository remains authoritative. Agents maintain beacon comments alongside their code changes, and a map refresh brings those changes into Workflowy. The map is a projection of the code, not a second implementation to maintain by hand.

> 📸 **SCREENSHOT — NEXUS reading its own code:** In Workflowy's map of this repository, render the **`glimpse-render`** beacon slice into the docked panel, headings-only. This is the Slice Catalog's “GLIMPSE render pipeline (server side)” topic. Show the mapped file/function hierarchy on the left and three or four readable signatures with their beacon comments on the right, spanning at least two files. Keep the slice tag visible. Crop out absolute machine paths and unrelated outline branches. Use the real implementation, not a mockup.

## Why Workflowy?

Because the shape of the memory and the way a human sees it belong together.

Workflowy is a nested outline inside one document. You can zoom into any bullet, move it, indent it, change its order, or tag it across organizational boundaries. A node can be a single sentence or a substantial document. NEXUS gives agents ways to address, select, render and maintain those same nodes—and puts source-code maps in that same space.

That combination matters:

- **Structure is visible.** You see a document and where it belongs at the same time.
- **Selections cross the hierarchy.** The context for a task need not match a folder boundary.
- **Order is something you choose.** Moving bullets changes the document-order render.
- **Restructuring is ordinary editing.** A growing project can change shape without turning every move into a file-management task.
- **The human has the same working surface.** An agent's record is something you can open and understand, not just an internal store you query through the agent.

Build a node-based context system from scratch and you would still want a way to browse its structure, edit its contents, tag its parts and render selected views. Workflowy already provides the outline; NEXUS connects it to the agents and the code.

**If Workflowy didn't exist, it would have to be invented.**

There is good work around this problem elsewhere. `CLAUDE.md` and `AGENTS.md` make instructions durable; Obsidian supports tags and queried notes; [Letta has shared, git-backed Markdown memory](https://www.letta.com/blog/context-repositories). Workflowy itself offers a [Desktop MCP](https://workflowy.com/help/mcp-server/), and [Tana has an MCP over its nodes and supertags](https://tana.inc/docs/local-api-mcp). NEXUS's contribution is the system built on this substrate: stable citations, curated context rendered for both human and agent, disk-backed code maps, and the harnesses that use them together.

## The mirror image: a harness built on its own tools

The two halves repeat one level up:

| | Knowledge work | Source-code work |
|---|---|---|
| **NEXUS tooling** | Anchor Docks, Anchor Points, slice groups, shared Markdown views | Cartographer, beacons, beacon slices, live snippets |
| **The harness built with it** | **Manager Sessions:** think, plan, remember and curate with an agent | **Program Conductor Toolkit:** dispatch workers, audit results and run remediation |

These are not just integrations on a diagram. Manager Sessions starts a conversation in an Anchor Dock and captures its durable conclusions as Anchor Points. The Toolkit gives each worker a **socket**: a Workflowy node containing its role, contract, inputs and permitted writes. The worker reads its assignment there and returns its result there. Briefs, results and decisions remain citable parts of the same record.

Both instruction systems are themselves **Workflowy subtrees**—living skills that agents load by reference. The Toolkit also runs knowledge-work programs: research and writing can use the same dispatch-and-return pattern as programming.

The companion **Manager App**, in its own repository, makes this visible on desktop and phone. It works with **OpenCode and Claude Code**, showing conversations, programs and workers; its View pane renders Workflowy material through NEXUS. You can think with the Manager while other agents work, then inspect what they brought back.

> 📸 **SCREENSHOT — From a plan to a running program:** Capture the Manager App on desktop while an open-source NEXUS development program is running. Expand the Programs tree far enough to show the Conductor and several workers, with real live status indicators and context readouts. In the View pane, open one of that program's Workflowy reports so the connection between the workers and their durable output is visible. Crop to the Programs and View panes; keep one report heading and its opening paragraph legible. No simulated activity.

**Continue with the [Manager App README](TODO-MANAGER-APP-README-URL).** [TODO Dan: replace this link with the public companion repository's README.]

**Read the skills themselves:** [TODO Dan: add the public Manager Sessions and Program Conductor Toolkit links, with their repository-readable editions.]

## What is here today

The core tooling, extension and harnesses are working systems used daily. Public packaging is at different stages:

| Component | What a reader will find |
|---|---|
| **NEXUS MCP server and its tool descriptions** | Working. Eight verb families cover nodes, anchors, tags, slices, rendering, source maps and system operations. |
| **GLIMPSE Chrome extension** | Working. The shared reading panel, Dock and slice controls, navigation and job-status widget. Currently installed by loading the extension unpacked. |
| **Cartographer and beacons** | Working. Repository maps, cross-file beacon slices and live symbol reads. |
| **Agent Kit** | En route. A standalone `code-map` skill using beacons directly from disk, without Workflowy or an MCP server. Built; repository tests and a Python smoke test passed in the component survey. |
| **Manager Sessions and Program Conductor Toolkit** | Working, used daily. Workflowy-native instruction systems; public editions and links still to be supplied. |
| **Manager App and its runtime tools** | Working, used daily in the author's setup. A separate repository covering the app, Claude Code host, OpenCode integration and the `oc_*` command-line and seat tools. |
| **Additional onboarding** | A small Workflowy skill file, a public introductory skill-subtree and a packaged Workflowy agent are planned. Agent instructions already exist for OpenCode and are composed for Claude Code; public-facing packaging is still to come. |

See **[Components and status](COMPONENTS.md)** for the fuller inventory. [TODO Dan: confirm the final location of the companion components file.]

### Start with the tools, not the whole harness

You can use NEXUS without adopting Manager Sessions or the Toolkit, and **you do not need a separate skill to start**. The tools teach their own use: a server introduction, eight always-available family descriptions, then per-operation mini-skills through `describe`.

```python
anchor(verb="describe", params={"detail": "index"})
anchor(verb="describe", params={"of": "point.capture", "detail": "full"})
```

The full Workflowy setup brings together your Workflowy account and API key, an MCP-capable host, the local NEXUS daemon, and the Chrome extension. The built-in descriptions are the operational reference. [TODO Dan: link the verified public installation guide.]

## Who made this

I'm **Daniel Nissenbaum**. I spent five years as a principal-level individual contributor at Microsoft. I've been far more active in private source than in open source; for the past several months I've been working continuously with agentic systems, finding ways to make them work better. This is what I've landed on.

This is one person's project, not a company. **Agents wrote the code. I drove the ideas and the architecture, and insisted on good design.** That is an advertising point: this is an agentic development system built using the system itself.

Workflowy is its own product, as are the agent runtimes. The initial seed was a minimal Workflowy MCP wrapper: **[CONFIRM Dan: vladzima/workflowy-mcp]**. The context-management system, code-mapping tools and harness described here grew from that starting point under my direction.

I'm not much of a marketer. But I have something I'd like to share—and this is the work.
