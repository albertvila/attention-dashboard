# Attention dashboard

One attention queue for GitHub and Jira, and one JSON snapshot
that every surface renders. `attention.py` owns the rules; `dashboard.html` is a
consumer, not a copy.

## Run it yourself

This is a **local, single-user tool**. Everyone runs their own copy, on their own
machine, authenticated as themselves: there is no shared server, no deploy, and
no snapshot anyone else reads. The repo is private — get access first.

Three CLIs, each authenticated as *you*:

| tool | what it answers for | how to get it |
|---|---|---|
| `python3` ≥ 3.9 | the producer and the server | usually already there; check `python3 --version` |
| `gh` | review requests, own PRs, assigned issues, specs | `gh auth login`, scopes `repo` + `read:org` |
| `twg` | Jira tickets, statuses, blockers | installed and authenticated (`twg auth`) |

Not one of them is a hard prerequisite. A missing CLI costs its source and
nothing else, and the page names the one to add: `⚠ jira tasks is off: twg is not
installed — this queue is partial`. The rest of the queue renders anyway. The
session rails are the one exception — they just go quiet, because a machine
without herdr or bb has no sessions to show, which is not a failure.

Then, from this directory:

```bash
python3 attention.py                        # server on http://127.0.0.1:8765
```

No snapshot exists on the first run, so the first page load cold-starts one
collection (~5s) before it renders; every read after that is instant and
file-backed. Nothing else has to be running — the page refreshes itself whenever
the snapshot it is showing is over an hour old.

### Configuration

One file: `config.json`, beside the snapshot. It is read per request, so an
edit lands on your next look with no restart, and it is gitignored — your knobs
are yours.

```json
{
  "specRepos": ["acme/checkout-api"],
  "stalkTeams": ["squad-platform", "team-payments"]
}
```

| key | default | what it does |
|---|---|---|
| `specRepos` | `[]` | repos where the **Specs** rail takes every `spec`-labelled issue, whoever wrote it — for reading a teammate's proposal before someone takes it. Everywhere else the rail is yours alone. With watched rows in play the rail splits into **Mine** and **Watched repos**, each row showing its writer's avatar, the login on hover. |
| `stalkTeams` | `[]` | GitHub team slugs whose members appear under Ongoing work. Click one to see their queue. Empty means no team list. |
| `stalker` | on | The switch over that whole feature: `false` hides the team faces and refuses `/queue`, so nothing is read as anyone else — your `stalkTeams` list stays as it is. Absent means on. |

### What needs your machine

Three surfaces want local state rather than a snapshot, and an absent tool costs
an empty rail, never the queue: **Ongoing work** (`/sessions`) reads herdr panes
and bb threads open on this machine, **`POST /focus`** moves your own terminal
window, and **`/reference`** is my workflow and skills page — not part of the
contract, ignorable if it is not your workflow.

### Known limits

- **A failed source still exits 0.** `python3 attention.py --json …` writes a
  snapshot and succeeds even when every source errored; the only signal is the
  `errors` array inside it, so a cron keyed on exit status never notices.
- **Ceilings:** 100 rows per GitHub search, and the 24h / 7-day windows the
  contract describes.
  Anything past a ceiling is absent, not error-flagged.
- **The server is unauthenticated.** It serves and writes on `127.0.0.1` only,
  and `/focus` moves windows on this machine — do not bind it to a LAN address.
- **Nothing personal travels.** `snoozes.json`, `acks.json` and `config.json` sit
  beside the snapshot and are gitignored, like `attention.json`: your parking and
  your knobs stay yours.

## The snapshot file

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

Only needed if you are writing a consumer — the dashboard, through
`attention-view.js`, is the one that exists today.

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
- `/comments?keys=RBT-1,FIRE-2` — the newest comments on those tickets, read live
  (one call per card, reused until the card has moved): who wrote, when, and what
  they said. A comment thread is not in the snapshot, for the same reason a
  session is not — it is not a property of "now" the way a card is
- `POST /focus` — open one session on this machine (`{kind: herdr|bb, target}`),
  the only write that moves a window rather than a file
- `/status` — this process's producer stamp versus the snapshot's
- `/attention-view.js` — the shared consumer rules the page loads
- `/reference` — the workflow and skills reference page; static, renders no
  snapshot

Reads never collect. That is the whole point: the file is the artifact every
surface consumes, and a refresh is one explicit producer run. One exception —
with no snapshot file yet, the first read cold-starts a single collection.

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
linked        [{key, url, status, status_category, type, summary}] — the Jira links that are not dependencies, open first; [] when there are none
chip          REVIEW | MY PR | ISSUE | JIRA | REVIEWED
title, ref, url, author, container, labels, detail, age
jira          ticket URL when the branch/item names one; jira_issue = {type,status,status_category,summary}
states        [{key, label, tier, tone}]  — tone: info|warn|bad|good|quiet
facts         [{label, tone}]
times         {updated, created} — raw ISO, for consumers that format their own
tier          needs | ready | waiting — the row's own tier, from its own states; a card's
              placement is its cluster's best state, so the two can differ (a live
              header waiting under a merged member reads tier=waiting, section=ready)
section       needs | ready | waiting | drafts | closed — where the row is rendered
children      linked items folded into this card (issue <-> PR <-> Jira)
change        {kind: new|moved, label, from_*/to_*} — THIS generation only
firstSeenAt   when the row first appeared; durable across generations
lastChangedAt when it last moved tier/state/section; durable
lastChange    that move, kept so a surface can label it later
```

Consumers should render `states`/`facts`/`change` as given and never re-derive
tiers, labels, tones, or ages — that is the whole point of the contract.

## A card is a cluster

Items that reference each other (issue ↔ PR ↔ Jira key) become
one card. **Open and closed items cluster together**, so a merged PR stays with its
ticket instead of splitting into the closed log. The **header** is chosen by kind,
not by urgency: a Jira ticket if the cluster has one, otherwise a GitHub issue
labelled `spec`, otherwise one labelled `ticket`, otherwise the PR. Anything else
only names the card when none of those are present. Within one kind, a live member
beats a finished one. The **tier** is still the cluster's best state, so a merged PR
lifts its card to **Ready when you are** even when a waiting ticket is the face.
Only groups whose members are all closed go to the closed log.

**Two Jira tickets for one incident** cluster through the Jira link itself. When
one of them is Fireline's alert ticket (the `FIRE-` project), the other names the
card and its state lands it: the alert's status is the automation's copy, not the
work's. A `blocks` / `is blocked by` link is a dependency, not sameness — the
blocker stays its own card and rides in `blocked_by`.

## With support (Jira)

A Jira ticket in `Support Investigating` has state `with-support`: it rides
**Waiting on others** like any other waiting work, labelled `with support` with
the Jira status as its fact. A ticket that has not started stays in Needs, and
`Waiting for Customer` stays `in-progress`.

## Blocked by (Jira)

A ticket whose Jira link type says `is blocked by` carries that blocker as
`blocked_by`, and the card renders it on its own line: the key as a link, the
blocker's type, its **status** and its summary — so "what am I waiting for" is
readable without opening Jira, and a blocker that has gone Done reads green
instead of amber. Jira hands all of it back with the link itself, so naming a
blocker costs no second call. The other direction (this card *blocks* another)
is not a block on this card and is not shown.

Every other Jira link is named the same way, in `linked`: the ticket you raised
with another team and are waiting on, a ticket of your own the incident also
touches, the work ticket an alert points at. One line each — key, its type, its
status and its summary — open tickets first, because the open one is the wait.
A link to a ticket the card already carries (a linked child, or the ticket the
header shows inline) is not named again: the same key on the same card twice is
noise.

## The next line on a card

A card you can act on says what it is asking for, in one sentence. Every line is
**a rule naming the evidence it stands on**, never an opinion: the kind of
evidence is what a rule reads, and the rule fires on what is there.

Five kinds are free — the state, the facts, the linked items, the Jira links,
the blockers, all of it already on the row. Three are reads: a ticket's
**comments**, a PR's **review comments**, and the **transitions Jira allows**
from the current status. A read costs one call per card, is reused until the card
has moved (`lastChangedAt` is the cache key), and a read that fails costs that
one line, never the queue. Comments are read today; the other two kinds are named
and silent — a rule that needs one is reported as waiting for it rather than
guessing, which is also how the board says "the answer would be in the comments".

A comment from somebody else, newer than your last look, is the strongest thing a
card can carry: a human is waiting on you, so it outranks the status. The first
look flags nothing — the same rule the change marks follow. Nothing here ever
moves a card, changes a tier or a count, and no model is asked anything: where no
rule fires, the card says so and names what it would take to have a line.

## Ongoing work and specs (read beside the snapshot)

Two things the board shows are **not** in the snapshot, because neither is true of
"now" the way a snapshot is: a session is machine state that changes minute to
minute, and a spec is a backlog nobody is waiting on. Both are read per request,
the way parking is — the page asks `/sessions` and `/specs`, the server answers,
nothing is stored, and a failed read costs a rail, never the queue.

**Sessions** come from two sources and say which one they came from. `herdr` is a
pane herdr itself recognized an agent in — or a pane sitting inside a
`~/.bb/plugins/<env>/host-data/{worktrees,workspaces}/thr_…` path, which is a bb
agent with a herdr pane, so it reports `origin: bb` and can be opened either way.
`bb` is a bb thread, including an `active` one running somewhere with no local
pane at all. Each session carries `busy` (an agent *working* right now, against a
pane merely left open), which the box's colour reads, and `herdr` / `bb` — the
tab to focus and the thread to open.

The right rail is one box per **repository** with a session open on it, and every
repository shows, whether or not the board carries a card for it — the rail is
machine state, not a view of the queue. The repository name is the whole join to
a card: it is on the card's own line and on the box. A Jira card names no
repository, so nothing joins to it. The repository is read from the checkout's own
git remote, so no name guessing is involved.

**Specs** are the open issues I wrote with the `spec` label and **no assignee**:
nobody has taken them, so they are groundwork rather than work. An assignee — and
it is usually me — means someone is already on it, which is exactly why those
tickets are already on the board. A repo named in `config.json`'s `specRepos` is
read for **anyone's** spec instead: the one read that reaches past my own work,
because a teammate's proposal is a thing I may want to read before somebody takes
it. Mine and the watched ones are two readings, so when both have something the
rail renders them as two labelled lists rather than one. Every row carries its
writer's GitHub avatar, the login on hover — the rail's only fetch from anywhere
but this machine, and offline a row simply loses its face. They render in a rail
beside the board and never in a tier: the queue by definition only carries work
someone needs now. One that the queue is showing anyway reads `already on the
board`, because a spec you are looking at is not a suggestion.

`POST /focus` is the one write that moves a window instead of a file: it runs
`herdr tab focus <tab>` — which raises Herdr itself — or `bb thread open <thread>`
followed by `open -a bb`, because bb's own CLI delivers the thread into the app
without bringing its window forward. Only for a target matching
`[A-Za-z0-9:_-]{1,64}`, and it is a POST so that loading a page can never move the
reader's terminal.

## Parking a card

Every card carries a quiet `⏰ snooze / ack` picker, always visible and only
styling up on hover:

- **4 hours / 1 day / 3 days / 1 week** — snoozed: parked until then.
- **until it changes** — acknowledged: hidden while the card has not moved, and
  back on the board the moment `lastChangedAt` passes your ack. This is the one
  for recurring noise you already know about.

Parked cards leave the tiers (and any fold) and collect in one collapsed
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
| Jira ↔ Jira | Jira **issue link**, either direction; `blocks` / `is blocked by` is a dependency (`blocked_by`), not membership | the Jira link itself |
| GitHub ↔ GitHub | `Closes owner/repo#123` (cross-repo) or `Fixes #123` | PR body — GitHub then reports it as a closing reference |
| GitHub ↔ GitHub | GitHub **issue/PR URL** | issue/PR body or comment |
| GitHub ↔ GitHub | sub-issue / parent, or `#123` in a PR **title** or an issue body | GitHub UI — `#1338` under `## Parent` is the link |

Bare keys in prose are ignored on purpose (`UTF-8`, `SHA-256` match the
`KEY-123` shape) — write the URL, or put the key in the branch name. A card that
should be merged and is not will show an empty `links`; that is the signal to add
one of the references above.

## Consumer rules: one implementation

`attention-view.js` is the only place the *reader-side* rules live — change
flags, ghosts (hidden in Needs), the Parked and Drafts folds, the fold summaries
(oldest age,
conflicted drafts, the parked kinds — built from the ages already on the rows,
never a second calculation), the next line a card asks for (see below), the
next-card line an empty Needs tier shows (the
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
timestamp — the dashboard keeps `gha.seenAt` in localStorage, so it is per
browser profile:

```
new       section != closed and firstSeenAt > myLastLook
changed   lastChangedAt > myLastLook   (label from lastChange.label)
dropped   goneAt > myLastLook          (ghost row; kept 7 days / 100 entries, and
                                       never rendered in the Needs queue — that
                                       tier only shows things you can act on)
```

Work that has closed is exempt from `new` wherever it rides: a card that closed
while you were away is first seen in Recently closed, and a merged PR that stays
as a member of a live card is first seen in that card — either way it closed, so
it is not a new card. Its `firstSeenAt` is the closure time, so its freshness is
still readable, and both render struck — the state badge already says how it
ended, so finished work never reads as something you just picked up. A card that
leaves a tier for that section still reports `moved` (`in progress → merged`).

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
  the producer stamp underneath, and the refresh rule after them: a snapshot
  older than an hour refreshes itself (live collection, ~5s) when the page takes
  focus — opening the tab, switching back to it — and, while the tab is visible,
  on a five-minute sweep, so a page left open on a screen does not rot. A hidden
  tab records nothing and collects nothing.
- **Needs you now** renders as generous blocks: title, states, facts, labels and
  the Jira line, then age, links and parking.
- **Ready when you are** and **Waiting on others** render as compact rows: the
  name and its states claim the first line, so the line under it — change mark,
  author, repository or key, facts, detail — starts at the same edge in every
  card, with age, links and parking at the row's right. Waiting does not
  collapse rows that share a title.
- **Parked**, **Drafts** and **Recently closed** are collapsed folds, each
  showing its count and the description the rules give it (`oldest 3d`,
  `snoozed until a time, or until the card changes`, …). A fold the rules have
  nothing to say about shows its count only.
- A card you can act on carries a **next line**: one sentence saying what it is
  asking for, from the evidence the board already has — a merged PR the ticket
  still waits on, a ready PR nobody has merged, a ticket to deploy, an approval
  you owe. It names that evidence on hover and never changes a tier or a count.
  A rule that asks you to do something shows wherever the card sits; a rule that
  only says you are waiting shows nowhere, because that is what the tier already
  means. A card in Needs or Ready with nothing to say says so — the answer is in
  something the board has not read.
- A card with linked items carries `N linked` at the row's right, beside age and
  parking, expanding them in place; a card with no links carries no count. Each
  child keeps its own chip, ref, states, facts, labels, detail and age — one
  card renderer serves every place a card appears, so no layout drops a part.
- A card that has left the queue renders as struck history — no state pills and
  no facts, just what it was and when it went — in the section it left.
- An empty **Needs you now** names the next card that could be picked up (the
  rules' next-card line) instead of reading as an empty queue.
- **Ongoing work** is a right rail: one box per repository with an agent session
  open on it — green while an agent is working, amber when blocked, grey when the
  pane is merely open — one row per session, and each row opens that session
  (`herdr` focuses the pane's tab, `bb` opens the thread). Sessions are read from
  `/sessions` at every look, so the rail is machine state, never a snapshot, and
  the repository name on the card is the whole join.
- **Specs** are a left rail: the open issues I wrote with the `spec` label that
  nobody has taken (no assignee) — plus, in the repos `config.json` names,
  everyone's, kept apart as **Mine** and **Watched repos** when both have
  something. Each row carries its writer's avatar with the login on hover, links
  to GitHub, and reads `already on the board` when the queue is already showing
  it. Browse material beside the board — never a tier, never a count, never
  parkable.
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
python3 -m unittest -v test_cli_seam     # the CLI seam, driven by a recorded adapter
python3 -m unittest -v test_view_model   # fixtures -> view model, contract, diff, parking
node test_attention_view.mjs             # the shared consumer rules
node test_dashboard_sweep.mjs            # the visible-tab sweep — and what a hidden tab must not do
```

Nothing here touches the network or a live CLI: the seam tests drive a recorded
adapter in place of `gh` and `twg`. (`node` for the two `.mjs` suites,
`python3` for the unittest modules.)
