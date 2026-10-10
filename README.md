# Attention dashboard

One attention queue for GitHub and Jira. `attention.py` collects it and writes one
JSON snapshot. `dashboard.html` renders that file. It does not decide what is true.

This is a **local, single-user tool**. You run your own copy, on your own machine,
authenticated as yourself. There is no shared server and no snapshot anyone else
reads. The code is in this repo. Your snapshot, your parking, and your config are
not: `attention.json`, `snoozes.json`, `acks.json`, and `config.json` are
gitignored.

**You** in this document is the person running the tool. `/reference` is the
author's own workflow page. Ignore it if it is not yours.

## Run it

| tool | required | what it is for |
|---|---|---|
| `python3` ≥ 3.9 | yes | the producer and the server |
| `gh` | for GitHub | review requests, your PRs, assigned issues, specs. `gh auth login`, scopes `repo` and `read:org` |
| `twg` | for Jira | tickets, statuses, blockers. Installed and authenticated (`twg auth`) |
| `herdr`, `bb` | no | the Ongoing work rail, and focusing a pane. A machine without them has an empty rail, which is not a failure |

A missing `gh` or `twg` costs that source and nothing else. The page names it:
`⚠ jira tasks is off: twg is not installed — this queue is partial`. The rest of
the queue still renders.

From this directory:

```bash
cp config.default.json config.json   # optional; skip it and you get the defaults
python3 attention.py                 # http://127.0.0.1:8765
```

No snapshot exists on the first run, so the first page load collects one (~5s)
and then renders. Every later read is the file. Nothing else has to be running.
While the tab is visible, a snapshot older than an hour refreshes itself, and the
page sweeps every five minutes. A hidden tab records nothing and collects nothing.

`python3 attention.py 9000` serves on another port.

## What you see

The page is a briefing. It reads top to bottom.

- A sentence of what is new, changed, and dropped since your last look, plus how
  many cards you can act on say nothing.
- **Needs you now** — blocks. The ball is yours.
- **Ready when you are** and **Waiting on others** — one line each.
- **Parked**, **Drafts**, and **Recently closed** — collapsed folds. A card in a
  fold is not in a tier count.
- An empty Needs names the next Ready card, instead of looking like an empty queue.
- A card you can act on has a **next line**: one sentence of what it is asking
  for. It never changes a tier or a count. In Needs and Ready, a card with
  nothing to say says so, and the sentence counts those (`· 2 say nothing`).
- **Ongoing work** is the right rail: one box per repository with an agent
  session open. Green while an agent is working, amber when blocked, grey when
  the pane is only open. The **Team** box sits above those, once you turn it on.
- **Specs** is the left rail: open issues you wrote with the `spec` label and no
  assignee. A backlog, not a queue. Not parkable, not counted.

Every card has a quiet `⏰ snooze / ack` picker. It only styles up on hover.

## Configuration

`config.json` sits beside the snapshot. It is read on each look, so an edit
lands without a restart, and it is gitignored. `config.default.json` is the
committed baseline. Copy it, or leave `config.json` out and you get exactly this:

```json
{
  "specRepos": [],
  "stalkTeams": [],
  "stalkBots": [],
  "githubOrg": "",
  "jiraBase": "",
  "stalker": false
}
```

| key | default | what it does |
|---|---|---|
| `specRepos` | `[]` | Repos where the Specs rail lists every `spec` issue, whoever wrote it. Everywhere else the rail is only yours. When both have rows, the rail splits into **Mine** and **Watched repos**. |
| `githubOrg` | `""` | The GitHub org `stalkTeams` are read from. Empty, and the Team box says so instead of looking empty. A slug in the wrong org is the same silence as a slug that does not exist: that team is skipped. |
| `stalkTeams` | `[]` | Team slugs in that org. The box groups by slug. Somebody on two teams stands under both. You are the first row and never appear in the groups. Empty means no roster. |
| `stalkBots` | `[]` | Logins to leave off the roster. Machine accounts have a login like anyone's and no `[bot]` suffix, so the one test every other read uses cannot catch them. Yours to name. |
| `jiraBase` | `""` | The Jira site, no path (`https://your-org.atlassian.net`). Ticket links are that site plus `/browse/KEY`. Empty means a key is not turned into a link. The code does not guess a company. |
| `stalker` | `false` | The switch over reading somebody else's queue. **Off unless `true`.** Off, the faces are not there and `GET /queue?login=` returns `{"error": "stalker is off"}`. Your team list stays where it is. |

## Using the board

### A card is a cluster

Items that name each other (issue, PR, Jira key) are one card. Open and closed
items cluster together, so a merged PR stays with its ticket. The **header** is
chosen by kind, not urgency: a Jira ticket if the cluster has one, otherwise a
GitHub issue labelled `spec`, otherwise one labelled `ticket`, otherwise the PR.
Within one kind, a live member beats a finished one. The **tier** is still the
cluster's best state, so a merged PR lifts its card to Ready even when a waiting
ticket is the face. Only a cluster whose members are all closed goes to Recently
closed.

Two Jira tickets for one incident cluster through the Jira link. When one is a
Fireline alert (`FIRE-`), the other names the card and its state lands it. A
`blocks` / `is blocked by` link is a dependency, not sameness: the blocker stays
its own card and rides on a `blocked by` line.

A Jira ticket in `Support Investigating` is `with support` and rides Waiting on
others. A ticket that has not started stays in Needs. `Waiting for Customer`
stays in progress.

### What a card is waiting on

A block link renders on the card: key, type, status, summary. A blocker that is
Done reads green. Jira returns that with the link, so naming it costs no second
call. The other direction (this card blocks something else) is not shown. The
test is the word "block" in the link type, because instances word it differently
(`is blocked by`, `Blocked by`).

Every other Jira link gets the same line, open tickets first. A key the card
already shows — a child, or the ticket in the header — is not named again.

### The next line

A card you can act on says what it is asking for, in one sentence. The sentence
is a rule standing on evidence, not an opinion.

Five kinds are already on the row: the state, the facts, the linked items, the
Jira links, the blockers. Two are reads: the ticket's **comments**, and the
**transitions** Jira allows from the current status. A read is one call per card,
reused until `lastChangedAt` moves, and a failed read costs that line, never the
queue. Comments are read today. The transitions read is named and silent: a rule
that needs it says it is waiting, which is also how the board says "the answer
would be in the comments".

A comment from somebody else, newer than your last look, outranks the status. The
first look flags nothing. Nothing here moves a card, a tier, or a count, and no
model is asked anything.

### Parking

- **4 hours / 09:00 tomorrow / 09:00 next Monday** — snoozed until that moment.
  The two mornings are moments in your timezone, not offsets. The server is
  handed the moment.
- **until it changes** — acknowledged. Hidden until `lastChangedAt` passes the
  ack. A reply counts as a move: on Jira that is the comment count, on GitHub the
  state already says who spoke last.

Parked cards leave the tiers and collect in one **Parked** fold, each with its
own `wake` / `unack`. The header still counts them.

Parking is your intent, not snapshot data. It lives in `snoozes.json`
(`{key: wake-up ISO}`) and `acks.json` (`{key: acked-at ISO}`). The producer does
not know about them. Expired snoozes fall out on the next write.

### When a card did not merge

Membership comes only from what the sources declare. There is no matching on
titles. A card that should be one and is not has an empty `links`. Add one of
these:

| direction | what links | where it must be written |
|---|---|---|
| GitHub → Jira | Jira key in a PR **branch name** (`m-feature-…-RBT-723`) | branch |
| GitHub → Jira | Jira **browse URL** | issue/PR body or comment |
| Jira → GitHub | GitHub **issue/PR URL** | Jira description |
| Jira ↔ Jira | Jira **issue link**, either direction. `blocks` / `is blocked by` is a dependency, not membership | the Jira link itself |
| GitHub ↔ GitHub | `Closes owner/repo#123` or `Fixes #123` | PR body — GitHub then reports it as a closing reference |
| GitHub ↔ GitHub | GitHub **issue/PR URL** | issue/PR body or comment |
| GitHub ↔ GitHub | sub-issue / parent, or `#123` in a PR **title** or an issue body | GitHub UI |

Bare keys in prose are ignored on purpose (`UTF-8` and `SHA-256` match the
`KEY-123` shape). Write the URL, or put the key in the branch name.

### Ongoing work, the team, specs

Sessions come from this machine, not from the snapshot. `herdr` is a pane herdr
recognized an agent in, or a pane inside a bb worktree path (that one reports
`origin: bb` and can be opened either way). `bb` is a bb thread, including an
active one with no local pane. `busy` is an agent working right now, which is
what the colour reads.

The repository name is the whole join to a card. It is read from the checkout's
git remote. A Jira card names no repository, so nothing joins to it.

Clicking a session runs `herdr tab focus` or `bb thread open`, then `open -a` to
raise the window. **Raising the window is macOS.** Without `open`, the jump still
happens and the window stays where it was. The target must match
`[A-Za-z0-9:_-]{1,64}`, and it is a POST so loading the page can never move your
terminal.

The Team box is the roster of `stalkTeams` in `githubOrg`. It is off until
`stalker` is `true`. Click a face to read that person's GitHub queue. Jira and
mail stay out of that view, because those tools are logged in as you.

Specs in `specRepos` are read for anyone, because a teammate's proposal is worth
reading before somebody takes it. Each row shows the writer's avatar. Offline, a
row simply loses its face. One the queue is already showing reads `already on the
board`.

## Known limits

- **A failed source still exits 0.** `python3 attention.py --json …` writes a
  snapshot even when every source errored. The signal is the `errors` array. A
  cron keyed on exit status never notices.
- **Ceilings.** 100 rows per GitHub search, and the 24h / 7-day windows the
  contract describes. Anything past a ceiling is absent, not flagged.
- **The server is unauthenticated.** It binds `127.0.0.1` only. `/focus` moves
  windows on this machine. Do not bind it to a LAN address.
- **A long-lived server can be running old code.** Every snapshot carries a
  `producer` stamp. `GET /status` compares it with the running process, and the
  page warns when they differ. Restart the server after you pull.

## If you are writing another surface

The dashboard, through `attention-view.js`, is the one surface today. A second
one should render `states`, `facts`, and `change` as given and never re-derive
tiers, labels, tones, or ages.

```bash
python3 attention.py --json attention.json   # write the file and print the delta
python3 attention.py --json -                # stdout; no previous snapshot to diff
python3 attention.py                         # server on :8765
python3 attention.py 9000                    # server on :9000
```

The snapshot file lives next to the script (`attention.json`) unless you pass a
path. It is generated state. Do not commit it.

The server adds:

- `/` — the dashboard
- `/attention.json`, `/api/queue` — the cached snapshot. No collection
- `/refresh` (GET or POST) — run the producer, rewrite the file, return the new
  snapshot. One run at a time
- `/snoozes`, `/acks` — the parking files. GET to read, POST to change
- `/sessions` — agent sessions open right now, per repository. Live, so it cannot
  be an hour stale
- `/specs` — open `spec` issues with no assignee, plus watched repos
- `/comments?keys=RBT-1,FIRE-2` — the newest comments on those tickets. Live, one
  call per card, reused until the card moves. A thread is not in the snapshot
- `/team` — the roster. `{you, people, error?}`. Empty, and no error, when
  `stalker` is off
- `GET /queue?login=` — that person's queue, computed now and not written down.
  `{"error": "stalker is off"}` when the switch is off. `{"error": "bad login"}`
  when the login is not a login
- `POST /focus` — `{kind: herdr|bb, target}`. The only write that moves a window
- `/status` — this process's producer stamp against the snapshot's
- `/attention-view.js` — the shared reader rules
- `/reference` — the author's workflow page. Static. Renders no snapshot

Reads never collect. With no snapshot file yet, the first read cold-starts one
collection.

Parking writes: `POST /snoozes` with `{key, until}` for a moment, `{key, hours}`
for an offset from now, `hours: 0` to wake. `POST /acks` with `{key}` to ack,
`{key, clear: true}` to unack.

Shape (`schema: 1`. Bump it when a field's meaning changes):

```
schema        int
generatedAt   ISO-8601 UTC
tiers         [{key: needs|ready|waiting, title, items: [row]}]
drafts        [row]        your PRs in draft: out of the tiers, never hidden
closed        [row]        merged or closed in the last 24h
hidden_bots   int          review requests whose author login ends in [bot]
errors        [{where, command, output}]
changes       {previousAt, summary:{new,moved,gone}, items:{key -> change}, gone:[row]}
```

Every row:

```
key           stable identity (acme/web-frontend#2040, FIRE-9001)
links         keys that clustered this card. Empty means standalone
blocked_by    [{key, url, status, status_category, type, summary}]
linked        the same shape, for Jira links that are not dependencies. Open first
chip          REVIEW | MY PR | ISSUE | JIRA | REVIEWED
title, ref, url, author, container, labels, detail, age
jira          ticket URL when the branch or the body names one
jira_issue    {type, status, status_category, summary}
states        [{key, label, tier, tone}]  — tone: info|warn|bad|good|quiet
facts         [{label, tone}]
times         {updated, created} — raw ISO
tier          needs | ready | waiting — from this row's own states
section       needs | ready | waiting | drafts | closed — where it renders.
              A card's placement is the cluster's best state, so the two can differ
children      linked items folded into this card
change        {kind: new|moved, label, from_*/to_*} — this generation only
comment_count comments the source reported for that ticket, or for the ticket a
              GitHub card names. The reply rule compares a card's tickets together,
              its own plus every member, so a reply on the alert under a card is a
              reply on the card. A reply landing is a move, label `a reply landed`,
              which is what wakes an acknowledged card. Absent when the source
              collects no comments, and those rows never move on one
firstSeenAt   when the row first appeared. Durable
lastChangedAt when it last moved tier, state, or section. Durable
lastChange    that move, kept so a surface can label it later
```

`url` on a Jira row is `jiraBase` plus `/browse/` plus the key. If `jiraBase` is
empty, `url` is empty. Do not invent a host.

### Reader rules

`attention-view.js` is the only place the reader-side rules live: change flags,
ghosts (hidden in Needs), the Parked and Drafts folds, fold summaries, the next
line, the next-card line an empty Needs tier shows, and where a card renders.
The dashboard loads it from `/attention-view.js`. `node test_attention_view.mjs`
checks it.

The producer owns what is true. This owns what you see on top of it.

### Change tracking, two layers

`with_changes()` diffs the current snapshot against the previous one by `key`:

- `new` — key absent before
- `moved` — state, tier, or section differs. `label` is ready to show
- `gone` — key present before, absent now. The previous row rides in
  `changes.gone` with its old `section` and a `goneAt` stamp

That is the per-generation delta. It is not what a person should see, because a
generation is not a look: the server collects per request, a cron collects per
tick, and anything in between eats the delta.

So rows also carry durable stamps, and each surface remembers its own last look.
The dashboard keeps `gha.seenAt` in localStorage, per browser profile:

```
new       section != closed and firstSeenAt > myLastLook
changed   lastChangedAt > myLastLook   (label from lastChange.label)
dropped   goneAt > myLastLook          (ghost; kept 7 days / 100 entries,
                                       never rendered in Needs)
```

Work that has closed is never `new`. Its `firstSeenAt` is the closure time, and
it renders struck. A card that leaves a tier for Recently closed still reports
`moved` (`in progress → merged`).

Ghosts are hidden in Needs because that tier is things you can act on. Other
tiers still ghost in place, except when every linked child is still live — then
the live card already names what the parent was.

The first look flags nothing. It only records the timestamp.

## Tests

```bash
python3 -m unittest -v test_cli_seam      # the CLI seam, driven by a recorded adapter
python3 -m unittest -v test_view_model    # fixtures -> view model, contract, diff, parking
node test_attention_view.mjs              # the shared reader rules
node test_dashboard_sweep.mjs             # the visible-tab sweep, and what a hidden tab must not do
node test_reference_page.mjs              # the reference page still says what it says
```

Nothing here touches the network or a live CLI. The seam tests drive a recorded
adapter in place of `gh` and `twg`.

The recording in `fixtures/gh_output.json` is invented on purpose. Real org,
repo, branch, and login names were taken out, and the shapes are what a recording
looked like. Re-capture it when a GraphQL selection changes: run the seam
commands against the live CLI, replace every identifier with an invented one,
keep the shapes, and commit that. A field that disappears upstream then shows up
as a diff here instead of as quietly missing data.
