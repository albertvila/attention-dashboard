# Attention queue

One queue for GitHub and Jira: sources emit items, linked items
cluster into one card, cards are classified into tiers, and the whole thing is
published as one snapshot every surface renders.

## Language

### The queue

**Source**:
A place signals come from — GitHub, Jira — normalised into items in the
same shape regardless of origin.
_Avoid_: provider, integration, feed

**Item**:
One normalised signal from a source, before classification: the shape adapters
emit.
_Avoid_: signal, event, entry

**Cluster**:
The set of items that link to each other by key. Open
and closed items cluster together.
_Avoid_: group, thread

**Card**:
How a cluster renders: one header, the rest as children.
_Avoid_: ticket, task, entry

**Alert ticket**:
The Fireline-opened `FIRE-` ticket Jira links to the ticket that caused an
incident. It rides as a child: the work ticket names the card and its state lands
it, never the alert's.
_Avoid_: incident

**Row**:
The snapshot's record of a card — the contract's unit, what a surface is handed
before styling.
_Avoid_: record

**State**:
Our source-agnostic vocabulary for what an item is doing (`review-requested`,
`ci-failing`, `merged`), each mapping to exactly one tier, label and tone.
_Avoid_: status (that is Jira's own field name, not our vocabulary)

**Tier**:
The urgency class a card lands in: needs, ready, waiting. A card takes the best
state in its cluster, so a merged member lifts a card whose live work waits; the
row's own `tier` is its header's, and `section` is where it renders. An Alert
ticket never decides it.
_Avoid_: bucket, priority, lane

**Section**:
Where a card actually renders: a tier, or drafts, or recently closed. A card in
the closed section can still hold a "needs" tier.
_Avoid_: list, group

**Chip**:
The source badge on a card (REVIEW, MY PR, ISSUE, JIRA, REVIEWED).
_Avoid_: badge (that is a state's pill, not its source)

**Fold**:
A collapsed home for cards that belong to neither a tier nor the closed log:
drafts, snoozed, acknowledged.

**With support**:
The state for a Jira ticket in `Support Investigating`. It rides the waiting
tier with every other ticket someone else holds.
_Avoid_: support section, support fold

### The snapshot

**Snapshot**:
The one JSON document every surface renders. The producer owns it; surfaces never
re-derive what is in it.
_Avoid_: cache, payload, state file

**Producer**:
The module that collects from sources, classifies, diffs against the previous
snapshot, and writes it.
_Avoid_: collector, backend, server

**Producer stamp**:
The producer-code hash in a snapshot, so a reader can tell when the file on disk
was written by different code than the process serving it.

**Generation delta**:
What moved between one snapshot and the previous one, by key: new, moved, gone.
For logs and digests — not for humans, because generations do not line up with
looks.
_Avoid_: diff, changes (that is the field name, not the concept)

**Durable stamps**:
Per-card `firstSeenAt`, `lastChangedAt` and `lastChange`, kept across
generations so a reader's own last look is the only thing it needs.
_Avoid_: history

**Last look**:
When a given reader last saw the queue — a surface's own record, not the
producer's. First look flags nothing.
_Avoid_: seen timestamp

**Ghost**:
A card that vanished, kept in the snapshot long enough for a reader who was away
to see it dropped, never in the needs tier.
_Avoid_: deleted card

### Parking

**Parking**:
User intent layered on top of a card without touching the snapshot. Two kinds,
both held in files beside it.
_Avoid_: muting, dismissal

**Snooze**:
Parked until a moment in time, then it comes back on its own.

**Acknowledge**:
Parked until the card itself changes, however long that takes.

### Structure

**View model**:
The tiered, ordered structure the producer derives from items — the pure seam
between collecting and rendering.
_Avoid_: presenter, presentation layer

**Surface**:
Whatever renders a snapshot and applies the reader rules: today the dashboard.
A surface decides nothing about what is true.
_Avoid_: client, frontend, UI

**CLI seam**:
The one seam every CLI call crosses — text, JSON and GraphQL alike — so the
producer's collection can be driven by a recorded adapter instead of the live
tools.
_Avoid_: runner, port, client, API layer
