# NEXUS

## Your agent's context is a document you can see.

**NEXUS is a Workflowy-native context-management system for agents and humans working together.** Projects, decisions, memory and source-code maps live in one shared outline. Choose the parts that matter; read them as one Markdown document; give that same view to an agent.

If you already think in Workflowy, this brings your agents into that workspace. If you build with agents, this is a reason to look at Workflowy: a way to make context something you can organize, inspect and hand off—not just something accumulating inside a conversation.

**Half knowledge work. Half source code. The same canvas for both.**

> 📸 **SCREENSHOT — The shared document:** In today's Workflowy Chrome extension, apply a neutral “Project launch” slice containing a project brief, a decision and a research note from different branches. Capture the Workflowy outline on the left and the docked Markdown panel on the right, with its node list above the rendered text. Show the same recognizable headings in both. Crop to this project and panel, with no unrelated sidebar entries or browser tabs.

### Point at context instead of describing it

Open a branch in Workflowy. Expand what matters. Ask your agent to **“GLIMPSE this.”** It reads the visible outline as Markdown. Or name a saved slice and have the server render it directly—without arranging the browser first.

The other direction works too. Ask the agent to **“show me that in the panel.”** It can render a selection into a reading pane beside your outline. This is the extension's **docked Markdown modal**: a document assembled from the selected nodes, not a separate copy you now have to maintain. Its upper pane is a navigable list; its lower pane is the rendered text.

You can read the whole selection, click into a section, or keep an applied slice on screen while you work elsewhere in the outline. The agent can read that panel's view back. Neither of you needs to translate what the other is looking at.

## Why an outline is a context system

The hard part of a long-running project isn't storing everything. It is knowing what the next conversation needs—and keeping that knowledge usable as the project changes.

Workflowy gives that problem an unusually good shape:

- **A node is a unit of memory.** It can hold a short decision or a full Markdown document in its note.
- **A hierarchy gives it a home.** Research, plans and working records remain legible as they grow.
- **Tags give it more than one use.** A decision can belong to this week's planning view and a future implementation brief without being copied.
- **Order is something you choose.** Move a bullet and the document's reading order changes. Indent or outdent to change its structure.
- **The structure is already visible.** You browse and edit the same outline the agents use.

NEXUS adds stable citations, curated views, rendering and source-code mapping to that foundation. The result is not an invisible memory store with a dashboard bolted on. **The memory is the workspace.**

Imagine the outline as a field of points: one set lights up for a planning agent, another for an implementer, another for a reviewer. They overlap where the work overlaps. Each agent gets a coherent document; everyone writes back into the same record.

That is context management made visible.

> “If Workflowy didn't exist, it would have to be invented.”
>
> That is the design conviction behind NEXUS: the storage, the structure and the human view belong together.

## Two halves, one way of working

### Knowledge work: capture now, curate for what comes next

**Anchor Docks are where things go. Slice groups are what you look at.**

A Dock is a named container for a project, a topic or a session. An **Anchor Point** is a node with a stable citation handle, such as `[ap:K7M2QF]`. Move it or rename it; the citation still finds it.

Talk through a project with an agent. Capture the useful conclusions as Anchor Points. Later, promote the durable ones into topic Docks, keeping a provenance trail. Tag the points that belong in a particular view and save that selection as a **slice group**.

For example, “Project launch” can bring together the approved scope, the latest design decision and the unresolved questions, even though they live in different branches. A new session reads that view, follows the citations it needs, and adds its own conclusions. The useful record survives the conversation.

The views can be as deliberate as the work requires: headings first, then full notes; recent arrivals or current attention; unions and intersections of selections; a search confined to the selected context. You choose the material without manufacturing another briefing document every time.

### Source code: give the agent a map with intent

Architecture rarely fits inside one file. The relevant functions may be scattered across a parser, an API handler, a cache and a renderer. A text search can find a name; it doesn't explain which pieces belong together.

**Cartographer maps a repository into Workflowy**, with nodes for folders, files, classes and functions. **Beacons** are small comments in the source that label the entry points worth finding and explain what they do. Those labels become tags on the map.

```python
# @beacon[
#   id=launch@load_plan,
#   slice_labels=launch-planning,
#   kind=ast,
#   comment=Loads a project plan and checks its required fields.,
# ]
def load_plan(path: str):
    ...
```

*Illustrative beacon, not a NEXUS API.*

A **beacon slice** brings the selected symbols together as signatures and comments. An agent gets a cross-file picture, then reads a function's live source by name with `beacon.snippet`. A Slice Catalog describes the available code topics so it can choose where to start.

**Disk is truth.** The code stays in its repository; Cartographer refreshes the Workflowy projection from it. Agents maintain beacon comments alongside their code changes—curating useful entry points, not tagging every helper. The explanation travels with the implementation instead of becoming another detached document.

> 📸 **SCREENSHOT — One feature across files:** Use NEXUS's own open-source repository map. Apply a rendering-related beacon slice that includes symbols from at least three files. Capture the mapped file/function hierarchy and the Markdown panel showing signatures with their beacon comments. Keep enough of each file heading visible to make the cross-file selection obvious; exclude local absolute paths and unrelated branches.

## The harness mirrors the tools

The same two halves appear in the agentic system built on top:

| NEXUS supplies | The harness builds on it |
| --- | --- |
| Knowledge capture, citations, curation and selected context | **Manager Sessions** — an agent helps you think, plan, remember and carry work between conversations. |
| Code maps, beacon slices and live source reads | **Program Conductor Toolkit** — a conductor dispatches workers, collects results, and runs implementation, audit and remediation programs. |

Both are substantial **Workflowy subtrees of instructions**. The skill lives in the outline, where an agent can load the part it needs and follow its references. The Toolkit also runs knowledge-work programs; the division is a useful symmetry, not a wall.

This is concrete coupling, not just a diagram. A Manager session creates a Dock and curates its Anchor Points. A conductor gives each worker a **socket**—an Anchor Point stating its task, context and permitted changes. Workers read code through slices and snippets, then put their results back into Workflowy. Plans, execution and the record of what happened are all addressable in the same tree.

The **Manager App**, in its own repository, provides the desktop and phone interface over OpenCode and Claude Code: conversation, programs, workers and a Workflowy View pane. That pane calls the NEXUS daemon to render the same kinds of documents as the extension. The swarm integration is an application of the shared canvas, not a separate story.

> 📸 **SCREENSHOT — From plan to running program:** In the Manager App's desktop Programs pane, show one program working on NEXUS itself, with its conductor and several worker rows visible, including active status and context telemetry. Beside it, show the View pane rendering that program's brief or results from Workflowy. Crop out other programs, account information and local addresses. Use a real running state, not a mock-up.

**Read the companion story:** [TODO Dan: link to the Manager App repository and README].

**Explore the instruction systems:** [TODO Dan: add the published Manager Sessions and Program Conductor Toolkit links, with repository-readable copies if published]. They are working systems today; the public reading links are still to be supplied. A public share is a browser view, not automatic access to its Anchor Points through another person's MCP account.

## What this adds to the current landscape

Curated instruction files, skills, retrieval and progressive disclosure are already important parts of agent tooling. Workflowy has its own Desktop MCP; Tana has a node-based MCP interface; Obsidian supports tags and queried views; Letta uses shared, git-backed Markdown context. NEXUS is not claiming to have invented memory, tags or agent access to an outliner.

Its bet is on **putting the pieces together in one human-visible workspace**: stable memory units, cross-cutting selections rendered as documents, source code mapped into the same outline, and a harness that uses that record to coordinate work.

This is a design demonstrated in daily use, not a claim of benchmarked superiority over well-curated files. If your question is “What exactly did we give this agent, and where did its conclusion go?”, the answer should be something you can open and read.

## What's here

| Component | What you get | Status |
| --- | --- | --- |
| **NEXUS MCP server** | Eight tool families for nodes, Docks, citations, tags, slices, rendering, beacons and system operations; a shared daemon and local broker | Working, used daily |
| **Cartographer and beacons** | Repository maps, topic slices and live source reads | Working, used daily |
| **GLIMPSE Chrome extension** | The shared Markdown panel, Dock and slice controls, tab targeting and job visibility | Working, used daily; installed unpacked today |
| **Agent Kit** | The standalone `code-map` skill: beacon slices and source reads from disk, without Workflowy or an MCP server | En route; first implementation built and tested, with public-use hardening still ahead |
| **Manager Sessions and Program Conductor Toolkit** | The two Workflowy-native instruction systems described above | Working, used daily; public packaging pending |
| **Additional skill and agent packaging** | A small Workflowy-loading skill, public instructional subtree and packaged Workflowy agent | Planned; not yet packaged |

The **MCP tool descriptions are the starting skill**: load the tools and an agent can discover their use directly. Each family has an overview and a `describe` operation for detailed help. No separate skill file is required to begin. The existing OpenCode agents files and Claude Code prompt builders support the author's daily harness; portable public packaging is separate work.

See [COMPONENTS.md](COMPONENTS.md) for the component-by-component status and repository guide. [TODO Dan: supply that companion file at publication.]

### Using the tools

These are the sorts of requests the tools support:

- “Read what I have open.”
- “Capture this decision in the project Dock.”
- “Show me the Project launch slice.”
- “Search only that slice for the unresolved questions.”
- “Read `load_plan` from the mapped repository.”

Underneath, every call is a family, a verb and a parameter object. For example, an agent can learn source reading with:

```text
beacon(verb='describe', params={'of': 'snippet', 'detail': 'full'})
```

The Workflowy side uses a Workflowy account and API key, the NEXUS server and the Chrome extension. The server runs natively on Windows, macOS or Linux; WSL clients connect to the Windows daemon. [TODO Dan: link the verified public installation guide.] The Manager App and swarm runtime have their own setup, covered in the companion repository.

## Who made this

I'm **Daniel Nissenbaum**. I spent five years as a principal at Microsoft, as an individual contributor. I've never been especially active in open source; I've been incredibly active in private source. I'm not much of a marketer. I've spent these months working continuously with agents, finding ways to make them work better, and this is what I've landed on.

**Agents wrote the code. I drove the ideas, the architecture and the insistence on good design.** A system for agentic work, built with agents using that system: that is the point, not a qualification.

This is one person's design effort, not a company product or a wrapper around an existing agent-swarm framework. Workflowy itself and its API are not mine. The initial foothold was a minimal Workflowy MCP example; the system described here grew from there. [CONFIRM Dan: vladzima/workflowy-mcp — confirm and complete the upstream attribution.]

If you're interested in the engineering, look at how the knowledge layer and the code layer meet. That is the work I wanted to share.
