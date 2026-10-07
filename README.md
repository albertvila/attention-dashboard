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
in-memory copy, so an external writer (the CLI, another collector) is picked up
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
- `/sessions` — every agent session open right now, per repository: herdr panes
  and active bb threads, read live so it can never be an hour stale
- `/specs` — the open issues I wrote with the `spec` label and no assignee: a
  backlog, not a queue
- `POST /focus` — open one session on this machine (`{kind: herdr|bb, target}`),
  the only write that moves a window rather than a file
- `/status` — this process's producer stamp versus the snapshot's
- `/attention-view.js` — the shared consumer rules the page loads
- `/reference` — the workflow and skills reference page; static, renders no
  snapshot

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
key           stable identity across snapshots (acme/web-frontend#2040, FIRE-9001)
links         the keys that clustered this card with others; empty = standalone
blocked_by    [{key, url, status, status_category, type, summary}] — what Jira says holds this card up; [] when nothing does
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

A thread becomes one card (`mail/<threadId>`). The **star is membership and
nothing else**; the state follows who sent the last message — `waiting-reply`
when the From address is the mailbox account, `needs-reply` when it is anyone
else (or missing). Cards render in a **collapsed Starred mail fold** rather
than the tiers — like drafts — so starred mail never inflates the Needs count;
expand the fold to read them, and its summary says `oldest <age> · <n> unread`. Otherwise:
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

## With support (Jira)

A Jira ticket in `Support Investigating` has state `with-support`: it stays in the
snapshot's **waiting** tier and renders in a collapsed **With support fold**
rather than the Waiting queue, so a ticket parked with support does not inflate
the rendered Waiting count. The fold summary names the oldest age. A ticket that
has not started stays in Needs, and `Waiting for Customer` stays `in-progress` —
the support status is the only one that folds.

## Blocked by (Jira)

A ticket whose Jira link type says `is blocked by` carries that blocker as
`blocked_by`, and the card renders it on its own line: the key as a link, the
blocker's type, its **status** and its summary — so "what am I waiting for" is
readable without opening Jira, and a blocker that has gone Done reads green
instead of amber. Jira hands all of it back with the link itself, so naming a
blocker costs no second call. The other direction (this card *blocks* another)
is not a block on this card and is not shown.

## Ongoing work and specs (read beside the snapshot)

Two things the board shows are **not** in the snapshot, because neither is true of
"now" the way a snapshot is: a session is machine state that changes minute to
minute, and a spec is a backlog nobody is waiting on. Both are read per request,
the way parking is — the page asks `/sessions` and `/specs`, the server answers,
nothing is stored, and a failed read costs the columns, never the queue.

**Sessions** come from two sources and say which one they came from. `herdr` is a
pane herdr itself recognized an agent in — or a pane sitting inside a
`~/.bb/plugins/<env>/host-data/{worktrees,workspaces}/thr_…` path, which is a bb
agent with a herdr pane, so it reports `origin: bb` and can be opened either way.
`bb` is a bb thread, including an `active` one running somewhere with no local
pane at all. Each session carries `busy` (an agent *working* right now, against a
pane merely left open), which the column's colour reads, and `herdr` / `bb` — the
tab to focus and the thread to open.

A session joins a card by **repository**, read from the checkout's own git remote,
so no name guessing is involved. A Jira card names no repository, so nothing joins
to it: for `FIRE-*` and friends the column never appears.

**Specs** are the open issues I wrote with the `spec` label and **no assignee**:
nobody has taken them, so they are groundwork rather than work. An assignee — and
it is usually me — means someone is already on it, which is exactly why those
tickets are already on the board. They render in a rail beside the board and never
in a tier: the queue by definition only carries work someone needs now. One that
the queue is showing anyway reads `already on the board`, because a spec you are
looking at is not a suggestion.

`POST /focus` is the one write that moves a window instead of a file: it runs
`herdr tab focus <tab>` or `bb thread open <thread>`, only for a target matching
`[A-Za-z0-9:_-]{1,64}`, and it is a POST so that loading a page can never move the
reader's terminal.

## Parking a card

Every card carries a quiet `⏰ snooze / ack` picker, always visible and only
styling up on hover:

- **4 hours / 1 day / 3 days / 1 week** — snoozed: parked until then.
- **until it changes** — acknowledged: hidden while the card has not moved, and
  back on the board the moment `lastChangedAt` passes your ack. This is the one
  for recurring noise you already know about.

Parked cards leave the tiers (and the Mail fold) and collect in one collapsed
**Parked** fold, both kinds together and each row with its own `wake` / `unack`.
The header line still counts them, so a parked card never disappears silently.

Both are user intent, not data, so they live beside the snapshot in
`snoozes.json` (`{key: wake-up ISO}`) and `acks.json` (`{key: acked-at ISO}`) and
**the snapshot is never touched** — the producer does not know about them. Every
surface applies the same rule at read time. Expired snoozes fall out on the next
write.

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
flags, ghosts (hidden in Needs, never for mail or support), the With
support/Starred mail/Parked folds, the fold summaries (oldest age, unread mail,
conflicted drafts, the parked kinds — built from the ages already on the rows,
never a second calculation), the next-card line an empty Needs tier shows (the
first Ready card, or nothing when Ready is empty too), and where a card renders.
The dashboard loads it (the server serves it at `/attention-view.js`), so the
surfaces cannot drift apart.
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
The other tiers (and drafts/closed) still ghost in place — except when every
linked child is still live, because then the live card already names what the
parent was and the struck copy is the same work twice.

First look flags nothing and just records the timestamp. Nothing needs to run on
a schedule for this to work: whenever the next snapshot arrives, it already
contains enough history for the reader to see what it missed.

## Surface

**Dashboard** (`dashboard.html`, served at `/` by `attention.py`) — the
**Briefing**: a document that reads top to bottom. Every read comes from the
producer at `/api/queue`, and every decision about *where* a card renders comes
from `attention-view.js`.

- It opens with the day and the rules' summary sentence — what is new, changed
  and dropped since your last look (`gha.seenAt`) — with the snapshot age and
  the producer stamp underneath, and the refresh rule after them: when the page
  takes focus — opening the tab, switching back to it — a snapshot older than an
  hour refreshes itself (live collection, ~20s), never on a timer.
- **Needs you now** renders as generous blocks: title, states, facts, labels and
  the Jira line, then age, links and parking.
- **Ready when you are** and **Waiting on others** render as compact rows: one
  line while the name leaves room, otherwise the name takes the width it needs
  and key, facts, age and parking move to the line below.
  Waiting renders the groups the rules hand back: rows sharing a title opening
  collapse behind `key · N items — the opening they share, counted`; rows that
  share none are plain rows, however they were bucketed.
- **Starred mail**, **With support**, **Parked**, **Drafts** and **Recently
  closed** are collapsed folds, each showing its count and the description the
  rules give it (`oldest 3d · 2 unread`, `snoozed until a time, or until the
  card changes`, …). A fold the rules have nothing to say about shows its count
  only.
- A card with linked items carries `N linked` at the row's right, beside age and
  parking, expanding them in place; a card with no links carries no count. Each
  child keeps its own chip, ref, states, facts, labels, detail and age — one
  card renderer serves every place a card appears, so no layout drops a part.
- A card that has left the queue renders as struck history — no state pills and
  no facts, just what it was and when it went — in the section it left.
- An empty **Needs you now** names the next card that could be picked up (the
  rules' next-card line) instead of reading as an empty queue.
- A card whose repository has an **agent session open on it** gets a column
  outside the card, beside it: one row per session — green while an agent is
  working, amber when blocked, grey when the pane is merely open — and each row
  opens that session (`herdr` focuses the pane's tab, `bb` opens the thread).
  A card with no session keeps its full width, which is what makes the column
  itself the signal. Sessions are read from `/sessions` at every look.
- **Specs** are a left rail: the open issues I wrote with the `spec` label that
  nobody has taken (no assignee), each linking to GitHub, marked `already on the
  board` when the queue is already showing it. Browse material beside the board —
  never a tier, never a count, never parkable.
- Parking is an explicit control on every card and writes the same `snoozes.json`
  / `acks.json` files as ever; a parked card still counts in the header line.

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
