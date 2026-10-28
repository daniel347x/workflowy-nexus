# 🌱 Pantry · example workspace

A made-up team building **Pantry**, a shared grocery-list app. Example data for the NEXUS README: three Anchor Docks in three branches, and slice groups that cut across them.

## 📱 Product

### 🚀 Pantry 2.0 release #wfy-ns

The 2.0 release: what's in it and the checklist to ship. One node per feature; design notes go in the note.

#### [ap:Q9Z518] Offline lists: sync design #offline

**Goal:** a list keeps working with no signal, and nothing typed in the aisle is lost.

- Every edit is saved on the phone first, then queued.
- The queue syncs the moment the phone is back online.
- Two people changed the same item? The last edit wins; a delete wins over an edit.

| Case | Result |
|---|---|
| Edited offline, then synced | Applied in order |
| Same item edited on two phones | Last edit wins |
| Deleted on one phone, edited on the other | Deleted |

Agreed in the design review [ap:LP6AKR].

#### ✅ Release checklist

##### Offline QA in a basement supermarket #offline

Airplane mode is not enough: test where the signal really drops.

## 🤝 Customers

### 💬 Customer feedback #wfy-ns

What customers tell us: support tickets, store reviews and interviews, one node per theme. Agents summarize new tickets here.

#### "My list disappears in the store": 14 tickets this month #offline

> "No signal in the basement aisle, and my list was blank."
> "Lost three items I added in the parking garage."

Pattern: underground stores, rural shops, parking garages. All on version 1.9.