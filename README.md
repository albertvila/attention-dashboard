# Attention dashboard

One attention queue for GitHub, Jira and starred mail, and one JSON snapshot
that every surface renders. `attention.py` owns the rules; `dashboard.html` is a
consumer, not a copy.

The snapshot file lives next to this script (`attention.json`) unless a path is
given: the server reads it, `/refresh` rewrites it, and the page fetches it.
Plain `attention.py` is enough — the CLI write form is for scripts and crons.
It is generated state, not source, so it is not committed.

Every snapshot also carries a `producer` stamp — `{code, version}`, where `code`
is a hash of the collector that wrote it. `GET /status` reports the running collector's stamp next to the
snapshot's, and every surface shows a warning when they differ: that is the
witness for a long-lived server rewriting the file with old rules, or a snapshot
that predates the code. A changed file mtime also makes the server drop its
in-memory copy, so an external writer (the CLI, the panel's Refresh) is picked up
instead of being served over.

## The snapshot contract

```bash
python3 attention.py --json attention.json   # write + print the delta
python3 attention.py --json -                # stdout (no previous to diff)
python3 attention.py                         # server on :8765
python3 attention.py 9000                    # server on :9000
```

The server adds:

- `/` — the dashboard page; every read is instant and file-backed
- `/attention.json`, `/api/queue` — the cached snapshot (no collection)
- `/refresh` (GET or POST) — runs the producer, rewrites the shared file,
  returns the new snapshot; one run at a time
- `/snoozes`, `/acks` — the parking files (GET, and POST to change them)
- `/status` — this process's producer stamp versus the snapshot's
- `/attention-view.js` — the shared consumer rules the page loads

Reads never collect. That is the whole point: the file is the artifact every
surface consumes, and a refresh is one explicit producer run. If no file exists
yet, the first read cold-starts with one collection.

Shape (`schema: 1`, bump it when a field's meaning changes):

```
schema        int, contract version
generatedAt   ISO-8601 UTC
tiers         [{key: needs|ready|waiting, title, items: [row]}]
drafts        [row]        own PRs in draft: out of the tiers, never hidden
closed        [row]        merged/closed in the last 24h
hidden_bots   int          bot-authored review requests filtered out
errors        [{where, command, output}]
changes       {previousAt, summary:{new,moved,gone}, items:{key -> change}, gone:[row]}
```

Every row carries:

```
key           stable identity across snapshots (Launchmetrics/BIT-databricks#2040, FIRE-95364)
links         the keys that clustered this card with others; empty = standalone
chip          REVIEW | MY PR | ISSUE | JIRA | MAIL | REVIEWED
title, ref, url, author, container, labels, detail, age
jira          ticket URL when the branch/item names one; jira_issue = {type,status,status_category,summary}
states        [{key, label, tier, tone}]  — tone: info|warn|bad|good|quiet
facts         [{label, tone}]
times         {updated, created} — raw ISO, for consumers that format their own
tier          needs | ready | waiting (the row's own tier; children derive from states)
section       needs | ready | waiting | drafts | closed — where the row is rendered
children      linked items folded into this card (issue <-> PR <-> Jira)
change        {kind: new|moved, label, from_*/to_*} — THIS generation only
firstSeenAt   when the row first appeared; durable across generations
lastChangedAt when it last moved tier/state/section; durable
lastChange    that move, kept so a surface can label it later
```

Consumers should render `states`/`facts`/`change` as given and never re-derive
tiers, labels, tones, or ages — that is the whole point of the contract.

## Mail (gmcli)

Every starred thread is in the queue, archived ones included:
`is:starred -in:trash` (override with `MAIL_QUERY`, account with
`MAIL_ACCOUNT`). Un-star a thread and it drops out on the next refresh —
archiving does not, and the dashboard never writes to Gmail.

A thread becomes one card (`mail/<threadId>`, state `needs-reply`). Cards render in a
**collapsed Mail fold** rather than the tiers — like drafts — so starred mail never
inflates the Needs count; expand the fold to read them. Otherwise:
with `unread` / `N messages` / `attachment` as facts, the snippet as detail, and
a Gmail deep link. A Jira key or GitHub ref in the subject/snippet merges the
email into that ticket's card; a key that does not resolve is ignored (free text
matches `UTF-8` and `SHA-256` too).

**Merging threads that are not one Gmail thread**: apply the same
`group/<name>` label to several threads (multi-select in Gmail) and they become
one card, named by that label; the header is the most urgent member. Only
`group/`-prefixed *user* labels group anything — `Zoom`, `Later` and friends are
ignored — and the star stays the only way into the queue, so a label never
smuggles an item in. Each dashboard ends with a "How this queue works" section
that says the same thing.

A card is a cluster: items that reference each other (issue ↔ PR ↔ Jira key) become
one card, most urgent member first, the rest as children. **Open and closed items
cluster together**, so a merged PR stays with its ticket instead of splitting into
the closed log — and because the merged state ranks above waiting, that card moves
to **Ready when you are** with the ticket shown inline. Only groups whose members
are all closed go to the closed log.

## Parking a card

Every card carries a quiet `⏰ snooze / ack` picker, always visible and only
styling up on hover:

- **4 hours / 1 day / 3 days / 1 week** — snoozed: parked until then.
- **until it changes** — acknowledged: hidden while the card has not moved, and
  back on the board the moment `lastChangedAt` passes your ack. This is the one
  for recurring noise you already know about.

Parked cards leave the tiers (and the Mail fold) and collect in collapsed
**Snoozed** / **Acknowledged** folds, each with `wake` / `unack`. The header line
still counts them, so a parked card never disappears silently.

Both are user intent, not data, so they live beside the snapshot in
`snoozes.json` (`{key: wake-up ISO}`) and `acks.json` (`{key: acked-at ISO}`) and
**the snapshot is never touched** — the producer does not know about them. Every
surface applies the same rule at read time, so parking in the dashboard parks it
in the panel too. Expired snoozes fall out on the next write.

The backend writing those same files: `GET/POST /snoozes` and `GET/POST /acks`
on the server (POST `{key, hours}` or `{key, until}`; `hours: 0` wakes;
`{key, clear: true}` unacks).

## How linking works

A card is a cluster. Membership comes only from what the sources themselves
declare — there is no fuzzy matching on titles — and it is visible per row in
the snapshot's `links` field.

| direction | what links | where it must be written |
|---|---|---|
| GitHub → Jira | Jira key in a PR **branch name** (`m-feature-…-RBT-723`) | branch |
| GitHub → Jira | Jira **browse URL** | issue/PR body or comment |
| Jira → GitHub | GitHub **issue/PR URL** | Jira description |
| GitHub ↔ GitHub | `Closes owner/repo#123` (cross-repo) or `Fixes #123` | PR body — GitHub then reports it as a closing reference |
| GitHub ↔ GitHub | GitHub **issue/PR URL** | issue/PR body or comment |
| GitHub ↔ GitHub | sub-issue / parent, or `#123` in a PR **title** | GitHub UI |
| Mail | Jira key or GitHub URL in subject/snippet; `group/<name>` label | mail text, Gmail label |

Bare keys in prose are ignored on purpose (`UTF-8`, `SHA-256` match the
`KEY-123` shape) — write the URL, or put the key in the branch name. A card that
should be merged and is not will show an empty `links`; that is the signal to add
one of the references above.

## Consumer rules: one implementation

`attention-view.js` is the only place the *reader-side* rules live — change
flags, ghosts (hidden in Needs, never for mail), the Mail/Snoozed/Acknowledged
folds, and where a card renders. The dashboard loads it (the server serves it at
`/attention-view.js`), so the surfaces cannot drift apart.
`node test_attention_view.mjs` checks it.

The producer owns what is true; this owns what you see on top of it.

## Change tracking: two layers, on purpose

`with_changes()` in `attention.py` diffs the current snapshot against the
previous one by `key`:

- `new` — key absent before
- `moved` — state, tier, or section differs (`label` is a ready-to-show string)
- `gone` — key present before, absent now; the previous row rides in
  `changes.gone` with its old `section` and a `goneAt` stamp

That is the **per-generation delta** (logs, digests, "what happened since the
last run"). It is not what a human should see, because generations do not line
up with looks: the server collects per request, a cron collects per tick, and
anything else that collects in between eats the delta.

So rows also carry durable stamps, and each surface remembers its own last-look
timestamp (`gha.seenAt` in localStorage for the dashboard and the plugin):

```
new       section != closed and firstSeenAt > myLastLook
changed   lastChangedAt > myLastLook   (label from lastChange.label)
dropped   goneAt > myLastLook          (ghost row; kept 7 days / 100 entries, and
                                       never rendered in the Needs queue — that
                                       tier only shows things you can act on)
```

Recently closed is exempt from `new` on purpose: a card that closes while you
are away is first seen in that window, but it closed — it is not a new card. Its
`firstSeenAt` is the closure time, so its freshness is still readable. A card
that leaves a tier for that section still reports `moved` (`in progress → merged`).

Ghosts are hidden in the Needs queue for the same reason: it is the list of
things you can act on, and a struck-through row there reads as an open question.
The other tiers (and drafts/closed) still ghost in place.

First look flags nothing and just records the timestamp. Nothing needs to run on
a schedule for this to work: whenever the next snapshot arrives, it already
contains enough history for the reader to see what it missed.

## Surface

**Dashboard** (`dashboard.html`, served at `/` by `attention.py`) — the tiers,
collapsed Mail/Snoozed/Acknowledged/Drafts/Recently closed folds, the
`new` / `changed` / `dropped` marks, and the last-look timestamp
(`gha.seenAt`). Every read comes from the producer at `/api/queue`.

The harness (dsh) plugin that renders the same snapshot in a sidebar panel lives
outside this repo and is not published here; it reads `attention.json` per
request and runs the same producer on demand.

```bash
# write the snapshot yourself instead of pressing refresh
python3 attention.py --json attention.json
```

No cron needed: the flags describe the stretch since your last look whenever you
do refresh.

## Tests

```bash
python3 -m unittest -v test_view_model   # fixtures -> view model, contract, diff, parking
node test_attention_view.mjs             # the shared consumer rules
```
