#!/usr/bin/env python3
"""Attention queue — one queue for GitHub and Jira.

Run:  python3 attention.py     then open http://127.0.0.1:8765
      python3 attention.py --json attention.json   one shared snapshot

Standard library only, read-only (search/view/checks/GraphQL reads, never a
mutating call). Sources are adapters that emit normalized items
{source, container, states, detail, times}; classification into tiers and
ordering is source-agnostic, so a new source is an adapter, not a rewrite.

The JSON snapshot (--json, /attention.json) is the contract other surfaces
render: schema tag, generatedAt, the tiered view, and what changed since the
previous snapshot. Diffing happens here so no consumer re-implements it.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

# Every file this script reads or serves lives next to it.
HERE = os.path.dirname(os.path.abspath(__file__))

# --- settled vocabulary -----------------------------------------------------

NEEDS, READY, WAITING = "needs", "ready", "waiting"
TIERS = [(NEEDS, "Needs you now"), (READY, "Ready when you are"), (WAITING, "Waiting on others")]

# Source-agnostic: a state maps to (tier, label, tone) and nothing else, so a
# new source states its own vocabulary here. First tier in TIERS order that any
# of an item's states maps to wins (needs > ready > waiting).
STATES = {
    "review-requested": (NEEDS, "review", "info"),
    "needs-comments": (NEEDS, "needs comments", "warn"),
    "ci-failing": (NEEDS, "CI failing", "bad"),
    "conflicts": (NEEDS, "merge conflicts", "bad"),
    "needs-reply": (NEEDS, "needs reply", "warn"),
    "not-started": (NEEDS, "not started", "quiet"),
    "ready": (READY, "ready", "good"),
    "to-deploy": (READY, "to deploy", "info"),
    "merged": (READY, "merged", "good"),
    "done": (READY, "done", "good"),
    "waiting": (WAITING, "waiting", "quiet"),
    "waiting-reply": (WAITING, "waiting reply", "quiet"),
    "with-support": (WAITING, "with support", "quiet"),
    "in-progress": (WAITING, "in progress", "info"),
    "closed": (WAITING, "closed", "quiet"),
}

# What a row's first state reads once the work is finished (see _closed_pr_item,
# _closed_issue_item, items_from_jira_closed). A gone row carrying one of these
# belongs in the closed lane, not in the tier it was last rendered in.
CLOSED_STATES = ("merged", "closed", "done")


def _state(key):
    """One state -> the chip a surface renders; an unknown state reads as
    waiting, labelled with its own key."""
    tier, label, tone = STATES.get(key, (WAITING, key, "quiet"))
    return {"key": key, "label": label, "tier": tier, "tone": tone}

# Shared snapshot contract: bump when a field changes meaning or disappears.
SCHEMA_VERSION = 1
# Bumped by hand for human-meaningful changes; the code hash below is the
# automatic "which build wrote this file" witness a stale process is caught by.
PRODUCER_VERSION = 4
_PRODUCER_CODE = None


def producer_code():
    """Short hash of this file: a long-running server that keeps rewriting the
    snapshot with older code becomes visible to every reader."""
    global _PRODUCER_CODE
    if _PRODUCER_CODE is None:
        try:
            with open(os.path.abspath(__file__), "rb") as fh:
                _PRODUCER_CODE = hashlib.sha256(fh.read()).hexdigest()[:12]
        except OSError:
            _PRODUCER_CODE = ""
    return _PRODUCER_CODE

GREEN_CHECK = {"SUCCESS", "NEUTRAL", "SKIPPED"}
# gh reports running checks as their status (IN_PROGRESS), not a conclusion.
PENDING_CHECK = {"PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "REQUESTED", "WAITING"}


def _check_state(check):
    """green / pending / failed — only failed blocks the PR."""
    state = check.get("state")
    if state in GREEN_CHECK:
        return "green"
    return "pending" if state in PENDING_CHECK else "failed"

PR_FIELDS = "number,title,repository,author,createdAt,updatedAt,isDraft,url,labels"
ISSUE_FIELDS = "number,title,repository,createdAt,updatedAt,url,commentsCount,labels"
# `author` rides on every spec row: the rail shows the writer's avatar, and its
# title is the name — no second call, GitHub serves `<login>.png`.
SPEC_FIELDS = "number,title,repository,url,updatedAt,assignees,author"

JIRA_BASE = "https://launchmetrics.atlassian.net/browse/"
JIRA_KEY = re.compile(r"[A-Z][A-Z0-9]+-\d+")
# Done-category statuses that still need you (deploy work that isn't finished).
DEPLOY_STATUSES = ("TO_DEPLOY",)
# Waiting on a support engineer: still waiting on others.
SUPPORT_STATUS = "Support Investigating"
JIRA_JQL = ("assignee = currentUser() AND (statusCategory != Done OR "
            + " OR ".join(f'status = "{s}"' for s in DEPLOY_STATUSES)
            + ") ORDER BY updated DESC")
JIRA_FIELDS = ("summary,status,issuetype,description,updated,created,project,assignee,issuelinks")
# The open read asks for the comments too, and the count is all it takes from
# them: Jira's status never shows a reply, so how many comments a ticket carries
# is the only witness that somebody wrote to you (see `_replied`).
JIRA_OPEN_FIELDS = JIRA_FIELDS + ",comment"
# statuscategorychangeddate = when the ticket entered the Done category.
JIRA_CLOSED_FIELDS = JIRA_FIELDS + ",statuscategorychangeddate"
# The inward text of the link type that holds a ticket up: the linked issue is
# the blocker, and Jira hands back its status and summary with the link itself.
JIRA_BLOCKED_BY = "is blocked by"
# Fireline opens an alert ticket in the FIRE project and links it to the ticket
# that owns the work ("Problem/Incident": the work ticket causes the alert).
# The alert is the automation's copy of the incident, not the card.
JIRA_ALERT_PREFIX = "FIRE-"
# Two horizons, on purpose. A closed row is *shown* in Recently closed for a day,
# and it is *collected* for as long as a ghost can appear (GONE_WINDOW_HOURS), so a
# live card keeps its closed members for as long as the memory of them matters —
# otherwise a member expires, falls out of its card, and ghosts into the tier it
# left, as work that is still owed.
CLOSED_WINDOW_HOURS = 24
# Gone rows stay in the snapshot long enough for a surface that was not open when
# they vanished; consumers filter by their own last-look timestamp.
GONE_WINDOW_HOURS = 24 * 7
CLOSED_MEMORY_HOURS = GONE_WINDOW_HOURS
MAX_GONE = 100

JIRA_CLOSED_JQL = ("assignee = currentUser() AND statusCategory = Done "
                   "AND statuscategorychangeddate >= -%dd " % (CLOSED_MEMORY_HOURS // 24) +
                   "AND status not in (" + ", ".join(f'"{s}"' for s in DEPLOY_STATUSES) + ") "
                   "ORDER BY statuscategorychangeddate DESC")
# GitHub issue/PR URLs inside Jira text -> owner/repo#number links.
GITHUB_REF = re.compile(r"https?://github\.com/([^/\s\"'<>]+)/([^/\s\"'<>]+)/(?:issues|pull)/(\d+)")
# Jira ticket URLs inside GitHub text (issue bodies) -> KEY-123 link keys.
JIRA_REF = re.compile(r"https?://[^\s\"'<>]*atlassian\.net/browse/([A-Z][A-Z0-9]+-\d+)")
# Bare #123 refs in GitHub text (PR titles) -> same-repo issue/PR links.
ISSUE_REF = re.compile(r"(?<![\w/])#(\d+)")

# --- the pure seam: normalized items in, view model out ---------------------

# Copied verbatim into cluster children; firstSeenAt/lastChangedAt/lastChange are
# stamped later by with_changes, straight onto the child dicts.
# `title` rides along for a child's hover text, and for the link text of one that
# names no ref.
CHILD_FIELDS = ("chip", "key", "ref", "title", "url", "states", "age", "detail", "facts", "labels", "times")


def _closed_updated(group):
    """The newest closure stamp in a cluster: `times.updated` is the closure for
    every closed source, and a cluster is as fresh as its freshest member."""
    return max((i.get("times", {}).get("updated") or "" for i in group), default="")


def build_view(items, hidden_bots=0, errors=None, now=None, closed=None):
    """Normalized items -> tiered, ordered view model. No I/O, no gh.
    Linked items (issue/PR refs, Jira keys) become one card. The header is a
    Jira ticket if the cluster has one, else a `spec` issue, else a `ticket`
    issue, else a PR. The cluster's best state still decides the tier, so a
    merged PR lands the card in Ready, where the follow-up is.
    A cluster whose members are all closed goes to the closed log."""
    now = now or datetime.now(timezone.utc)
    closed_items = list(closed or [])
    closed_ids = {id(item) for item in closed_items}   # no marker on the caller's dicts
    buckets = {key: [] for key, _ in TIERS}
    drafts = []
    closed_rows = []
    for group in _clusters(list(items) + closed_items):
        rows = [_row(item, now) for item in group]
        # Jira, then a spec issue, then a ticket issue, then a PR. Finished
        # work only names the card when nothing preferred is still live.
        rows.sort(key=_header_key)
        # The tier is the cluster's best state, not the header's. A merged PR is
        # the evidence that the live member's own state is behind reality — a
        # ticket still In Review, an issue nobody replied to — so the card lands
        # where the finished member points, next to the follow-up (close the
        # ticket, deploy it), instead of buried among everything that waits.
        # A Fireline alert is the exception: while the ticket that owns the work
        # is in the cluster, the alert's status is the automation's copy, not
        # the work's, so it does not decide where the card lands either.
        owned = any(r["chip"] == "JIRA" and not r["_alert"] for r in rows)
        lands = min(rows, key=lambda r: (owned and r["_alert"], r["_rank"]))
        # the header's own Jira ticket is already shown inline; don't repeat it.
        jira_key = rows[0]["jira"].rsplit("/", 1)[-1] if rows[0]["jira"] else ""
        kids = [r for r in rows[1:] if r["ref"] != jira_key]
        # A link to a ticket this card already carries — a child, or the ticket it
        # names inline — is not a line of its own: the key would render twice.
        carried = {r["ref"] for r in rows} | {jira_key}
        rows[0]["linked"] = [l for l in rows[0].get("linked") or [] if l["key"] not in carried]
        rows[0]["children"] = [{k: r[k] for k in CHILD_FIELDS} for r in kids]
        if all(id(item) in closed_ids for item in group):
            # The lane shows the last day, however long closed work is collected
            # for: a finished cluster older than that drops out, and a surface
            # that was not open when it left sees it struck in the lane.
            if _within_window(_closed_updated(group), now):
                closed_rows.append(rows[0])
        else:
            # drafts are tests/POCs: out of the attention tiers, into their own section.
            (drafts if rows[0]["draft"] else buckets[lands["tier"]]).append(rows[0])
    for rows in list(buckets.values()) + [drafts]:
        # stalest activity first.
        rows.sort(key=lambda r: r["_updated"])
        for r in rows:
            del r["_updated"]
            del r["_rank"]
            del r["_alert"]
    closed_rows.sort(key=lambda r: r["_updated"], reverse=True)
    for r in closed_rows:
        del r["_updated"]
        del r["_rank"]
        del r["_alert"]
    return {
        "tiers": [
            {"key": key, "title": title, "items": buckets[key]} for key, title in TIERS
        ],
        "drafts": drafts,
        "closed": closed_rows,
        "hidden_bots": hidden_bots,
        "errors": errors or [],
    }


def _row(item, now):
    states = item.get("states") or ["waiting"]
    chips = [_state(s) for s in states]
    tier = next((t for t, _ in TIERS if t in [c["tier"] for c in chips]), WAITING)
    row = {
        "chip": item.get("chip", ""),
        "key": _item_key(item),
        "title": item.get("title", ""),
        "ref": item.get("ref", ""),
        "url": item.get("url", ""),
        "author": item.get("author", ""),
        "jira": item.get("jira", ""),
        "links": item.get("links") or [],   # what clustered this card; empty = on its own
        "blocked_by": item.get("blocked_by") or [],   # what holds it up, in its own line
        "linked": item.get("linked") or [],   # Jira links that are not dependencies, named
        "jira_issue": item.get("jira_issue"),
        "container": item.get("container", ""),
        "labels": item.get("labels") or [],
        "facts": item.get("facts") or [],
        "states": chips,
        "detail": item.get("detail", ""),
        "age": humanize_age(item.get("times", {}).get("updated"), now),
        "times": {k: v for k, v in (item.get("times") or {}).items() if v},
        "draft": bool(item.get("draft")),
        "children": [],
        "tier": tier,
        # Fireline's own alert ticket, for the header/landing preference below.
        "_alert": item.get("source") == "jira" and str(item.get("ref") or "").startswith(JIRA_ALERT_PREFIX),
        "_updated": _utc(item.get("times", {}).get("updated")),
        "_rank": [t for t, _ in TIERS].index(tier),
    }
    if item.get("comment_count") is not None:
        row["comment_count"] = item["comment_count"]
    return row


def _item_key(item):
    if item.get("key"):
        return item["key"]
    if item.get("source") == "jira":
        return item.get("ref", "")
    return f'{item.get("container", "")}#{item.get("ref", "").rsplit("#", 1)[-1]}'


def _clusters(items):
    """Union items that reference each other (issue/PR refs, Jira keys) into
    clusters; unlinked items come back as one-item groups."""
    parent = {}

    def find(k):
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for item in items:
        key = _item_key(item)
        find(key)
        links = list(item.get("links") or [])
        if item.get("jira"):
            links.append(item["jira"].rsplit("/", 1)[-1])
        for link in links:
            union(key, link)

    groups = {}
    for item in items:
        groups.setdefault(find(_item_key(item)), []).append(item)
    return list(groups.values())


def _utc(iso):
    """Sort key: ISO-8601 in UTC, so string order == time order across Z/+0200."""
    if not iso:
        return ""
    return _parsed(iso).astimezone(timezone.utc).isoformat()


def humanize_age(iso, now):
    if not iso:
        return ""
    secs = max(0, (now - _parsed(iso)).total_seconds())
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    days = secs / 86400
    if days < 30:
        return f"{int(days)}d"
    if days < 365:
        return f"{int(days // 30)}mo"
    return f"{int(days // 365)}y"


# --- the shared snapshot contract -------------------------------------------
# One JSON shape every surface renders: other tools consume this instead of
# re-deriving the rules. The diff lives here too, so change detection is
# written once rather than per consumer.

def _iso_utc(dt):
    """datetime -> the contract's timestamps: UTC, ISO-8601, Z-suffixed."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _row_key(row):
    """Stable identity across snapshots; survives re-tiering and re-clustering."""
    if row.get("key"):
        return row["key"]
    container = row.get("container") or ""
    if container:
        return f'{container}#{str(row.get("ref", "")).rsplit("#", 1)[-1]}'
    return row.get("ref", "")


_PR_CHIPS = ("MY PR", "REVIEW", "REVIEWED")


def _header_key(row):
    """Who names the card, most preferred first. A Jira ticket beats a spec
    issue, which beats a ticket issue, which beats a PR. Within one kind, a
    live member beats a finished one, then urgency, then age."""
    chip = row.get("chip") or ""
    names = {str(l.get("name", "")).lower() for l in (row.get("labels") or [])}
    if chip == "JIRA":
        kind = 0
    elif chip == "ISSUE" and "spec" in names:
        kind = 1
    elif chip == "ISSUE" and "ticket" in names:
        kind = 2
    elif chip in _PR_CHIPS:
        kind = 3
    else:
        kind = 4
    return (kind, _row_state(row) in CLOSED_STATES, row["_alert"],
            row["_rank"], row["draft"], row["_updated"])


def _row_state(row):
    states = row.get("states") or []
    return states[0].get("key", "") if states else ""


def _row_state_label(row):
    states = row.get("states") or []
    return states[0].get("label", "") if states else ""


def _row_tier(row):
    if row.get("tier"):
        return row["tier"]
    for state in row.get("states") or []:
        if state.get("tier"):
            return state["tier"]
    return WAITING


def _replied(was_row, row):
    """How the card's comment count moved since the previous snapshot: +1 when a
    reply landed, -1 when one went, 0 when nothing about it changed.

    A reply is a change to the card, and on Jira it is the only one the status
    cannot show — which is exactly the change an ack should wake for. It is a
    witness only when the row it is compared against carried a count as well:
    the first snapshot after this rule sets the baseline, instead of calling
    every ticket somebody has ever commented on newly changed."""
    before, after = was_row.get("comment_count"), row.get("comment_count")
    if before is None or after is None or before == after:
        return 0
    return 1 if after > before else -1


def _move_label(was_row, row):
    """Human summary of a move: the state change when there is one, the tier
    change otherwise (a second state — conflicts — can move a row alone), a reply
    when neither moved. A section-only move (a merged card leaving the closed
    log) has none of them, and must not read as 'Ready when you are → Ready when
    you are'."""
    from_label, to_label = _row_state_label(was_row), _row_state_label(row)
    if from_label != to_label:
        return f"{from_label} → {to_label}"
    from_tier, to_tier = _row_tier(was_row), _row_tier(row)
    if from_tier != to_tier:
        titles = dict(TIERS)
        return f"{titles.get(from_tier, from_tier)} → {titles.get(to_tier, to_tier)}"
    if _replied(was_row, row) > 0:
        return "a reply landed"
    return "moved"


def _iter_rows(view):
    """(section, row) for every card a surface renders, children included.
    Every section, drafts and closed too: a child that rode into the closed log
    with its parent is still a row a surface draws, so the diff must see it —
    otherwise it is reported gone and surfaces ghost it back into the tier it
    left, as work that is still owed."""
    sections = [(t["key"], t["items"]) for t in view.get("tiers") or []]
    sections += [("drafts", view.get("drafts") or []), ("closed", view.get("closed") or [])]
    for section, rows in sections:
        for row in rows:
            yield section, row
            for child in row.get("children") or []:
                yield section, child



def with_changes(view, previous=None, now=None):
    """Annotate every row versus `previous` and return the change block.

    Two layers, on purpose:
      change        what moved in THIS generation (deltas for logs, digests)
      firstSeenAt   when the row first appeared
      lastChangedAt + lastChange
                    the last move, durable across generations — a surface
                    compares these to its own last-look timestamp to flag
                    "new/changed since I looked", whatever happened in between.
    """
    now = now or datetime.now(timezone.utc)
    stamp = _iso_utc(now)
    baseline = (previous or {}).get("generatedAt") or stamp
    changes = {"previousAt": (previous or {}).get("generatedAt"),
               "summary": {"new": 0, "moved": 0, "gone": 0},
               "items": {}, "gone": []}
    old = {}
    for section, row in _iter_rows(previous or {}):
        old[_row_key(row)] = (section, row)
    seen = set()
    for section, row in _iter_rows(view):
        key = _row_key(row)
        seen.add(key)
        # A card whose header names its own Jira ticket shows that ticket inline
        # instead of as a child, so the diff counts it as seen: it is still on
        # screen, and a header that moved must not report it as dropped work.
        inline = (row.get("jira") or "").rsplit("/", 1)[-1]
        if inline:
            seen.add(inline)
        row["section"] = section
        was = old.get(key)
        if was is None:
            if section == "closed":
                # A card in Recently closed is never "new": it closed, and first
                # sighting in the window is not a change. Its closure time is the
                # honest stamp; a tier->closed move still reports as moved below.
                row["firstSeenAt"] = row["lastChangedAt"] = \
                    (row.get("times") or {}).get("updated") or stamp
            else:
                row["change"] = {"kind": "new"}
                row["firstSeenAt"] = row["lastChangedAt"] = stamp
        else:
            was_section, was_row = was
            row["firstSeenAt"] = was_row.get("firstSeenAt") or baseline
            row["lastChangedAt"] = was_row.get("lastChangedAt") or row["firstSeenAt"]
            if was_row.get("lastChange"):
                row["lastChange"] = was_row["lastChange"]
            if (_row_state(was_row), _row_tier(was_row), was_section) == \
                    (_row_state(row), _row_tier(row), section) and not _replied(was_row, row):
                continue
            row["change"] = {
                "kind": "moved",
                "from_state": _row_state(was_row), "to_state": _row_state(row),
                "from_label": _row_state_label(was_row), "to_label": _row_state_label(row),
                "from_tier": _row_tier(was_row), "to_tier": _row_tier(row),
                "label": _move_label(was_row, row),
            }
            row["lastChange"] = dict(row["change"])
            row["lastChangedAt"] = stamp
        if row.get("change"):
            changes["items"][key] = dict(row["change"])
    fresh = []
    for key, (section, row) in old.items():
        if key in seen:
            continue
        gone = {k: v for k, v in row.items() if k != "change"}
        gone.update({"section": "closed" if _row_state(row) in CLOSED_STATES else section,
                     "goneAt": stamp, "change": {"kind": "gone"}})
        fresh.append(gone)
    carried = [g for g in ((previous or {}).get("changes") or {}).get("gone") or []
               if _row_key(g) not in seen
               and _within_window(g.get("goneAt"), now, hours=GONE_WINDOW_HOURS)]
    changes["gone"] = (fresh + carried)[:MAX_GONE]
    changes["summary"] = {
        "new": sum(1 for c in changes["items"].values() if c["kind"] == "new"),
        "moved": sum(1 for c in changes["items"].values() if c["kind"] == "moved"),
        "gone": len(fresh),
    }
    return changes


def payload(view, previous=None, now=None):
    """The shared snapshot: schema tag, freshness, change annotations, view."""
    now = now or datetime.now(timezone.utc)
    out = dict(view)
    out["schema"] = SCHEMA_VERSION
    out["producer"] = {"code": producer_code(), "version": PRODUCER_VERSION}
    out["generatedAt"] = _iso_utc(now)
    out["changes"] = with_changes(view, previous, now)
    return out


# --- adapters: recorded gh JSON -> normalized items -------------------------

def _item(chip, *, source="github", key="", container="", ref="", title="", url="", author="",
          jira="", links=None, blocked_by=(), linked=(), states=(), detail="", facts=(), labels=(), times=None,
          draft=False, comments=None):
    """The one item shape every adapter emits, and only what its source has.
    Tier, label and tone come later from `states` — never here."""
    item = {
        "source": source,
        "chip": chip,
        "container": container,
        "ref": ref,
        "title": title,
        "url": url,
        "author": author,
        "jira": jira,
        "links": list(links or []),
        "blocked_by": list(blocked_by or []),
        "linked": list(linked or []),
        "states": list(states),
        "detail": detail,
        "facts": list(facts or []),
        "labels": list(labels or []),
        "times": dict(times or {}),
        "draft": draft,
    }
    if key:
        item["key"] = key
    # Only a source that actually collected them carries the count: a row without
    # it can never move on a comment, which is what keeps every other source's
    # changes exactly as they were.
    if comments is not None:
        item["comment_count"] = int(comments)
    return item


def items_from_review_search(rows):
    items, hidden = [], 0
    for row in rows:
        if row["author"]["login"].endswith("[bot]"):
            hidden += 1
            continue
        items.append(_review_item(row))
    return items, hidden


def _review_item(row):
    repo = row["repository"]["nameWithOwner"]
    jira, guess = _pr_jira(row.get("headRefName"), row.get("title"))
    item = _item(
        "REVIEW", container=repo, ref=_ref(repo, row["number"]), title=row["title"], url=row["url"],
        author=(row.get("author") or {}).get("login", ""), jira=jira,
        links=_github_links(row.get("closingIssuesReferences")) + _issue_refs_in(row.get("title"), repo),
        labels=_labels(row.get("labels")), states=["review-requested"], detail="review requested",
        times={"updated": row.get("updatedAt"), "created": row.get("createdAt")},
        draft=bool(row.get("isDraft")))
    if guess:
        item["jira_guess"] = True
    return item


def items_from_own_prs(rows, views, checks, threads, me=""):
    items = []
    for row in rows:
        key = _key(row)
        view = views.get(key)
        if not view or view.get("state") not in (None, "OPEN"):
            continue
        states = pr_states(view, checks.get(key, []), threads.get(key, {}), me)
        items.append(_pr_item(view, states, checks.get(key, []), threads.get(key, {})))
    return items


def pr_states(view, checks, threads, me=""):
    """All blockers, highest priority first; the first one drives the detail.
    Tier comes from the first state that maps to a tier (needs > ready > waiting)."""
    states = []
    comments = _unresolved_thread_comments(threads)
    if comments:
        # an unresolved thread only needs YOU when the ball is in your court
        states.append("needs-comments" if any(c["author"] != me for c in comments) else "waiting-reply")
    if view.get("mergeStateStatus") == "DIRTY":
        states.append("conflicts")
    if any(_check_state(c) == "failed" for c in checks):
        states.append("ci-failing")
    if not states:
        if view.get("reviewDecision") == "APPROVED" and view.get("mergeStateStatus") == "CLEAN" and not view.get("isDraft"):
            states.append("ready")
        else:
            states.append("waiting")
    return states


def _pr_item(view, states, checks, threads):
    repo = view["url"].split("/pull/")[0].split("github.com/")[-1]
    jira, guess = _pr_jira(view.get("headRefName"), view.get("title"), _text_of(view))
    item = _item(
        "MY PR", container=repo, ref=_ref(repo, view["number"]), title=view["title"],
        url=view["url"], author=(view.get("author") or {}).get("login", ""),
        jira=jira,
        links=list(dict.fromkeys(_github_links(view.get("closingIssuesReferences"))
                                 + _issue_refs_in(view.get("title"), repo)
                                 + _github_links_in(_text_of(view)))),
        labels=_labels(view.get("labels")), facts=_pr_facts(view, checks), states=states,
        detail=_pr_detail(view, states, checks, threads),
        times={"updated": view.get("updatedAt"), "created": view.get("createdAt")},
        draft=bool(view.get("isDraft")))
    if guess:
        item["jira_guess"] = True
    return item


def _pr_detail(view, states, checks, threads):
    parts = []
    for state in states:
        if state == "needs-comments":
            comments = _unresolved_thread_comments(threads)
            n = len(comments)
            last = comments[-1] if comments else {"author": "?", "text": ""}
            who = f'{last["author"]}: {last["text"]}' if last["text"] else last["author"]
            parts.append(f"{n} unresolved thread{'s' if n != 1 else ''} · {who}")
        elif state == "waiting-reply":
            n = len(_unresolved_thread_comments(threads))
            parts.append(f"{n} unresolved thread{'s' if n != 1 else ''} · waiting on reviewer")
        elif state == "ci-failing":
            bad = [c.get("name", "?") for c in checks if _check_state(c) == "failed"]
            parts.append(f"{len(bad)} not green · {bad[0]}")
        elif state == "waiting":
            reason = view.get("reviewDecision") or view.get("mergeStateStatus") or "in review"
            parts.append(reason.replace("_", " ").lower())
        # "conflicts"/"ready" are already spelled out by the fact chips
    return " · ".join(parts)


def _pr_facts(view, checks):
    """Plain status facts shown on the card: review, checks, merge."""
    decision = view.get("reviewDecision") or ""
    review = {"APPROVED": ("approved", "ok"),
              "CHANGES_REQUESTED": ("changes requested", "bad")}.get(decision, ("awaiting review", "warn"))
    bad = [c for c in checks if _check_state(c) == "failed"]
    pending = [c for c in checks if _check_state(c) == "pending"]
    test = ("checks green", "ok") if checks and not bad and not pending else \
           (f"{len(bad)} checks failing", "bad") if bad else \
           (f"{len(pending)} checks running", "warn") if pending else ("no checks", "warn")
    merge = {"CLEAN": ("mergeable", "ok"), "DIRTY": ("merge conflicts", "bad"),
             "BLOCKED": ("merge blocked", "warn"), "BEHIND": ("behind base", "warn")}.get(
        view.get("mergeStateStatus") or "", ("merge unknown", "warn"))
    return [{"label": label, "tone": tone} for label, tone in (review, test, merge)]


def items_from_issues(rows, views, linked, me):
    items = []
    for row in rows:
        key = _key(row)
        view = views.get(key)
        if not view:
            continue
        link = linked.get(key, {})
        states, open_prs = issue_states(view, link, me)
        items.append(_issue_item(view, states, open_prs, link))
    return items


def issue_states(view, linked, me):
    comments = view.get("comments") or []
    open_prs = [n for n in linked.get("closedByPullRequestsReferences", {}).get("nodes", []) if n.get("state") == "OPEN"]
    states = []
    if comments:
        states.append("needs-reply" if comments[-1]["author"]["login"] != me else "waiting-reply")
    if open_prs:
        states.append("in-progress")
    if not comments and not open_prs:
        states.append("not-started")
    return states, open_prs


def _issue_item(view, states, open_prs, link):
    repo = view["url"].split("/issues/")[0].split("github.com/")[-1]
    parts = []
    if "needs-reply" in states or "waiting-reply" in states:
        n = len(view.get("comments") or [])
        parts.append(f"{n} comment{'s' if n != 1 else ''}")
    for pr in open_prs:
        parts.append(f"PR #{pr['number']}")
    if not parts:
        parts.append("no comments, no PR")
    links = [f"{repo}#{link['parent']['number']}"] if link.get("parent") else []
    for group in ("subIssues", "closedByPullRequestsReferences"):
        links += [f"{repo}#{n['number']}" for n in (link.get(group) or {}).get("nodes", [])]
    # GitHub renders `#1338` in the body as a link. That is how a spec's tickets
    # name their parent (`## Parent` / `#1338`) when the parent field was not set.
    links += _github_links_in(_text_of(view))
    links += _issue_refs_in(_text_of(view), repo)
    return _item("ISSUE", container=repo, ref=_ref(repo, view["number"]), title=view["title"],
                 url=view["url"], links=list(dict.fromkeys(links)), jira=_jira_url_in(_text_of(view)),
                 states=states, detail=" · ".join(parts), labels=_labels(view.get("labels")),
                 times={"updated": view.get("updatedAt"), "created": view.get("createdAt")})


def _jira_blockers(row):
    """The tickets this one is blocked by, as the card shows them: key, its own
    status, type and summary. All of it arrives with the link, so naming a
    blocker costs no second call."""
    out = []
    for link in row.get("issuelinks") or []:
        if JIRA_BLOCKED_BY not in ((link.get("type") or {}).get("inward") or "").lower():
            continue
        issue = link.get("inwardIssue") or {}
        key = issue.get("key")
        if not key:
            continue
        fields = issue.get("fields") or {}
        status = fields.get("status") or {}
        out.append({
            "key": key,
            "url": JIRA_BASE + key,
            "status": status.get("name", ""),
            "status_category": (status.get("statusCategory") or {}).get("name", ""),
            "type": (fields.get("issuetype") or {}).get("name", ""),
            "summary": fields.get("summary", ""),
        })
    return out


def _jira_links(row):
    """The other side of every Jira link that is not a dependency, named the way
    a blocker is: key, its own type, status and summary, all of it handed back
    with the link itself. A blocking link is not here — it is `blocked_by`."""
    out = []
    seen = set()
    for link in row.get("issuelinks") or []:
        kind = link.get("type") or {}
        if "block" in (kind.get("inward") or "").lower() \
                or "block" in (kind.get("outward") or "").lower():
            continue
        for side in ("inwardIssue", "outwardIssue"):
            issue = link.get(side) or {}
            key = issue.get("key")
            # two link types to the same ticket is still one line on the card.
            if not key or key in seen:
                continue
            seen.add(key)
            fields = issue.get("fields") or {}
            status = fields.get("status") or {}
            out.append({
                "key": key,
                "url": JIRA_BASE + key,
                "status": status.get("name", ""),
                "status_category": (status.get("statusCategory") or {}).get("name", ""),
                "type": (fields.get("issuetype") or {}).get("name", ""),
                "summary": fields.get("summary", ""),
            })
    # still open first: that one is the wait, a finished one is history.
    out.sort(key=lambda l: l["status_category"] == "Done")
    return out


def _jira_linked_keys(row):
    """The other tickets a Jira link joins to this one, either direction: a link
    is how Jira says two tickets are the same work (Fireline's alert to the
    ticket that caused it). A blocking link is a dependency, not sameness — the
    blocker stays its own card and rides in this one's blocked_by line."""
    out = []
    for link in row.get("issuelinks") or []:
        kind = link.get("type") or {}
        if "block" in (kind.get("inward") or "").lower() \
                or "block" in (kind.get("outward") or "").lower():
            continue
        for side in ("inwardIssue", "outwardIssue"):
            key = (link.get(side) or {}).get("key")
            if key:
                out.append(key)
    return out


def _jira_comment_count(row):
    """The ticket's comment count, off the read itself (`comment.total`) — the
    bodies are not collected, and this is the one number that says a reply
    landed. None when the read did not ask for comments, so a Jira read that
    leaves the field out is not a ticket with no replies."""
    comment = row.get("comment")
    return comment.get("total") if isinstance(comment, dict) else None


def _jira_item(row, *, states, detail="", facts=(), blocked_by=(), updated=None):
    """A Jira row -> the item every Jira card shares. The two callers differ only
    in what they say about the ticket: its states, its detail line, its facts, and
    which stamp counts as `updated`."""
    desc = row.get("description")
    key = row["key"]
    return _item(
        "JIRA", source="jira", ref=key, title=row.get("summary", ""),
        url=row.get("url") or JIRA_BASE + key,
        links=_github_links_in(desc if isinstance(desc, str) else json.dumps(desc))
              + _jira_linked_keys(row),
        blocked_by=blocked_by, linked=_jira_links(row), states=states, detail=detail, facts=facts,
        comments=_jira_comment_count(row),
        times={"updated": _iso(updated or row.get("updated")), "created": _iso(row.get("created"))})


def items_from_jira(rows):
    """Jira work items (already fetched) -> normalized items. Title is the summary."""
    items = []
    for row in rows:
        status = row.get("status") or {}
        status_name = status.get("name", "")
        category = (status.get("statusCategory") or {}).get("name", "")
        if category == "Done" and status_name not in DEPLOY_STATUSES:
            continue
        itype = row.get("issueType") or row.get("issuetype") or {}
        if status_name in DEPLOY_STATUSES:
            state = "to-deploy"
        elif status_name == SUPPORT_STATUS:
            state = "with-support"
        elif category == "In Progress":
            state = "in-progress"
        else:
            state = "not-started"
        tone = "warn" if status_name in DEPLOY_STATUSES else \
               {"Done": "ok", "In Progress": "warn"}.get(category, "waiting")
        items.append(_jira_item(
            row, states=[state], detail=itype.get("name", ""),
            facts=[{"label": status_name, "tone": tone}] if status_name else [],
            blocked_by=_jira_blockers(row)))
    return items


def _closed_pr_item(node, chip):
    """The same link rule as the open read: a key in the branch, a browse URL in
    the body, or — as a guess — a bare key in the title. A PR must not lose its
    ticket the moment it merges."""
    repo = node["repository"]["nameWithOwner"]
    jira, guess = _pr_jira(node.get("headRefName"), node.get("title"), node.get("body"))
    item = _item(
        chip, container=repo, ref=_ref(repo, node["number"]), title=node["title"], url=node["url"],
        author=(node.get("author") or {}).get("login", ""), jira=jira,
        links=_graphql_links((node.get("closingIssuesReferences") or {}).get("nodes"))
              + _issue_refs_in(node.get("title"), repo),
        labels=_labels((node.get("labels") or {}).get("nodes")),
        states=["merged" if node.get("state") == "MERGED" else "closed"],
        times={"updated": node.get("closedAt"), "created": node.get("createdAt")})
    if guess:
        item["jira_guess"] = True
    return item


def _closed_issue_item(node):
    repo = node["repository"]["nameWithOwner"]
    links = _graphql_links([node["parent"]] if node.get("parent") else [])
    for group in ("subIssues", "closedByPullRequestsReferences"):
        links += _graphql_links((node.get(group) or {}).get("nodes"))
    links += _issue_refs_in(node.get("body"), repo)
    return _item("ISSUE", container=repo, ref=_ref(repo, node["number"]), title=node["title"],
                 url=node["url"], links=list(dict.fromkeys(links)), jira=_jira_url_in(node.get("body")),
                 labels=_labels((node.get("labels") or {}).get("nodes")), states=["closed"],
                 times={"updated": node.get("closedAt"), "created": node.get("createdAt")})


def items_from_jira_closed(rows):
    """Tickets that entered the Done category recently (deploy statuses excluded)."""
    items = []
    for row in rows:
        status = (row.get("status") or {}).get("name", "")
        itype = ((row.get("issueType") or row.get("issuetype")) or {}).get("name", "")
        items.append(_jira_item(
            row, states=["done"], detail=" · ".join(p for p in (status, itype) if p),
            updated=row.get("statuscategorychangeddate") or row.get("updated")))
    return items


def _unresolved_thread_comments(threads):
    """Last commenter + one-line snippet of each unresolved thread."""
    out = []
    for node in threads.get("reviewThreads", {}).get("nodes", []):
        if node.get("isResolved"):
            continue
        nodes = node.get("comments", {}).get("nodes", [])
        if nodes:
            last = nodes[-1]
            out.append({"author": (last.get("author") or {}).get("login", "?"),
                        "text": _snippet(last.get("body"))})
    return out


def _graphql_links(nodes):
    """GraphQL link nodes with repository -> owner/repo#number keys."""
    return [f'{n["repository"]["nameWithOwner"]}#{n["number"]}' for n in nodes or [] if n.get("repository")]


def _snippet(text, limit=90):
    """One-line preview of a review comment."""
    line = " ".join((text or "").split())
    return line[:limit] + ("…" if len(line) > limit else "")


def _ref(repo, number):
    return f"{repo.split('/')[-1]}#{number}"


def jira_url(branch):
    """Jira key is the last KEY-123 token in the branch name, if any."""
    keys = JIRA_KEY.findall(branch or "")
    return JIRA_BASE + keys[-1] if keys else ""


def _jira_url_in(text):
    """Jira ticket URL inside GitHub text (issue body) -> browse URL; first wins.
    URL-only on purpose: a bare KEY-123 token false-positives on UTF-8/SHA-256."""
    m = JIRA_REF.search(text or "")
    return JIRA_BASE + m.group(1) if m else ""


def jira_key_in(title):
    """The first bare KEY-123 token in a PR title. A guess, not a link: a title
    can cite an older ticket ("revert RBT-336 hack"), so `_with_jira` unlinks a
    guess Jira does not resolve instead of warning about it."""
    keys = JIRA_KEY.findall(title or "")
    return JIRA_BASE + keys[0] if keys else ""


def _pr_jira(branch="", title="", body=""):
    """A PR's Jira link, and whether it is a guess. Deliberate first — a key in
    the branch, then a browse URL in the body — then a bare key in the title."""
    deliberate = jira_url(branch) or _jira_url_in(body)
    if deliberate:
        return deliberate, False
    guess = jira_key_in(title)
    return guess, bool(guess)


def _labels(objs):
    """GitHub labels -> {name, color}; drops ids and descriptions we don't show."""
    return [{"name": l.get("name", ""), "color": l.get("color", "")} for l in (objs or [])]


def _github_links(refs):
    """closingIssuesReferences nodes -> owner/repo#number keys."""
    out = []
    for ref in refs or []:
        repo = ref.get("repository") or {}
        owner = (repo.get("owner") or {}).get("login", "")
        if ref.get("number") and repo.get("name") and owner:
            out.append(f'{owner}/{repo["name"]}#{ref["number"]}')
    return out


def _github_links_in(text):
    """GitHub issue/PR URLs inside text -> owner/repo#number keys. Explicit URLs
    only: a bare #123 in prose is not a reference we can trust."""
    return [f"{owner}/{repo}#{num}" for owner, repo, num in GITHUB_REF.findall(text or "")]


def _text_of(view):
    """An item's own prose: body plus every comment. Written links in here are
    how a card gets linked by hand (e.g. "PR: <url>" as a comment)."""
    chunks = [view.get("body") or ""]
    chunks += [(c or {}).get("body") or "" for c in (view.get("comments") or [])]
    return "\n".join(chunks)


def _issue_refs_in(text, repo):
    """Bare #123 refs in GitHub text (PR titles) -> same-repo keys.
    A #123 is same-repo by GitHub's own rendering; owner/repo#123 is skipped."""
    return [f"{repo}#{num}" for num in ISSUE_REF.findall(text or "")]


def _iso(ts):
    """Jira sends +0200 offsets; fromisoformat wants +02:00 before py3.11."""
    return re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", ts) if ts else ts


def _parsed(iso):
    """ISO-8601 text -> an aware datetime. Accepts `Z` and Jira's `+0200`, so
    every reader of a stamp goes through one place."""
    return datetime.fromisoformat(_iso(iso).replace("Z", "+00:00"))


def _within_window(ts, now, hours=CLOSED_WINDOW_HOURS):
    """True when ts is inside the last `hours` — date-filtered searches over-fetch."""
    if not ts:
        return False
    return (now - _parsed(ts)).total_seconds() <= hours * 3600


def _key(row):
    return f'{row["repository"]["nameWithOwner"]}#{row["number"]}'


# --- live collection: CLI subprocesses ---------------------------------------

class CliError(RuntimeError):
    def __init__(self, command, output):
        self.command = command
        self.output = output
        super().__init__(output)


STALK_BOTS = {"bit-github-lm", "lm-sec-github", "lm-qinfei", "maxlaunchmetrics"}
STALK_LOGIN = re.compile(r"^[A-Za-z0-9-]{1,39}$")
STALK_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")


class Cli:
    """The one seam every CLI call crosses: text, JSON and GraphQL alike.
    Production uses LIVE; a test injects a recorded adapter, so a whole
    collection can run without touching the live tools."""

    def text(self, command, args):
        with CLI_SLOTS:
            proc = subprocess.run([command] + args, capture_output=True, text=True)
        if proc.returncode != 0:
            raise CliError(command + " " + " ".join(args), (proc.stderr or proc.stdout).strip())
        return proc.stdout

    def json(self, command, args):
        return json.loads(self.text(command, args))

    def graphql(self, query):
        return self.json("gh", ["api", "graphql", "-f", "query=" + query])


LIVE = Cli()

# A read is its own process on a network round trip, and waiting for each in turn
# was the whole cost of a refresh: seventy-odd of them, a second each, one after
# another, a minute of nothing. The sources and their cards now share one read
# each, and `map` walks the rows in order whatever order the answers land in, so
# the view never depends on who answers first.
CLI_IN_FLIGHT = 16
# One budget for every read in the process, not one per pool: the pools nest (a
# source runs its per-card reads inside the pool of sources, the page asks for
# its rails while a refresh is still going, and a teammate's queue is a whole
# refresh of its own), and what has to stay bounded is the number of CLI
# processes, not the number of threads waiting on them. Measured while a refresh
# was seventy calls: 10-12s with 8 in flight, 7-8s with 16, no better with 24.
CLI_SLOTS = threading.Semaphore(CLI_IN_FLIGHT)


def _each(fn, rows):
    """Run one read per row at once, in the order of `rows`. A failure inside
    `fn` is the caller's to handle — this only decides that the rows do not
    queue."""
    rows = list(rows)
    if not rows:
        return []
    with ThreadPoolExecutor(max_workers=min(CLI_IN_FLIGHT, len(rows))) as pool:
        return list(pool.map(fn, rows))


class AsThem(Cli):
    """The same reads, as someone else. GitHub queries use their login. Jira
    queries use their account id."""

    def __init__(self, login, account_id="", inner=None):
        self.login = login
        self.account_id = account_id
        self.inner = inner or LIVE

    def text(self, command, args):
        args = [a.replace("@me", self.login) for a in args]
        # Only the queries asking who *they* are need their account: a read by
        # key is the same ticket for whoever asks. Without an account those
        # who-am-I reads stay out — one skipped source, not a warning per ticket
        # the queue happens to link.
        if command == "twg" and any("currentUser()" in a for a in args):
            if not self.account_id:
                raise CliError(command, f"skipped — no Jira account matched {self.login}")
            args = [a.replace("currentUser()", '"%s"' % self.account_id) for a in args]
        return self.inner.text(command, args)


def collect_view(me=None, cli=None):
    """Run the live queries and build the view model. Read-only."""
    cli = cli or LIVE
    errors, items, hidden = [], [], 0
    now = datetime.now(timezone.utc)
    # The per-card helpers take gh JSON in argv shape; bind that call once off
    # the adapter so they need no second parameter.
    gh = partial(cli.json, "gh")
    me = me or _whoami(gh)

    def review_requests(errors):
        """What other people are waiting on me for: the search, then one query
        for the branch name and the issues each one closes."""
        found, bots = [], 0
        try:
            # user-review-requested, not review-requested: the latter also matches
            # team requests (a team you're in was asked), which is not your queue.
            rows = gh(["search", "prs", f"user-review-requested:{me or '@me'}", "--state=open",
                       "--json", PR_FIELDS, "--limit", "100"])
            query, keys = _graphql_for(rows, _REVIEW_SELECTION)
            answers = _answers(cli, query, keys)
            for row in rows:
                node = answers.get(_key(row))
                if node:
                    row["headRefName"] = node.get("headRefName")
                    row["closingIssuesReferences"] = (node.get("closingIssuesReferences") or {}).get("nodes")
            found, bots = items_from_review_search(rows)
        except Exception as e:
            errors.append(_error("review requests", e))
        return found, bots

    def my_prs(errors):
        """Mine, and how each one is doing: its view, its checks, its threads —
        one query for all of them, not three calls a card."""
        found = []
        try:
            rows = gh(["search", "prs", "--author=@me", "--state=open", "--json", PR_FIELDS, "--limit", "100"])
            query, keys = _graphql_for(rows, _PR_SELECTION)
            answers = _answers(cli, query, keys)
            views, checks, threads = {}, {}, {}
            for key, node in answers.items():
                views[key] = _pr_view(node)
                checks[key] = _checks_of(node)
                threads[key] = node            # the threads ride in the node itself
            found = items_from_own_prs(rows, views, checks, threads, me)
        except Exception as e:
            errors.append(_error("my PRs", e))
        return found, 0

    def assigned_issues(errors):
        """Issues on me: the issue itself and what would close it, in the same
        one query."""
        found = []
        try:
            rows = gh(["search", "issues", "--assignee=@me", "--state=open", "--json", ISSUE_FIELDS, "--limit", "100"])
            query, keys = _graphql_for(rows, _ISSUE_SELECTION, field="issue")
            answers = _answers(cli, query, keys, field="issue")
            views, linked = {}, {}
            for key, node in answers.items():
                views[key] = _issue_view(node)
                linked[key] = node             # parent, sub-issues, closing PRs
            found = items_from_issues(rows, views, linked, me)
        except Exception as e:
            errors.append(_error("assigned issues", e))
        return found, 0

    def jira_tasks(errors):
        """Every ticket assigned to me, open or waiting on a deploy."""
        found = []
        try:
            data = cli.json("twg", ["jira", "workitem", "query", "--jql", JIRA_JQL, "--limit", "100",
                                    "--fields", JIRA_OPEN_FIELDS,
                                    "--output", "json", "--output-summary", "none"])["data"]
            found = items_from_jira(data["issues"] if isinstance(data, dict) else data)
        except Exception as e:
            errors.append(_error("jira tasks", e))
        return found, 0

    def closed_window(errors):
        """Work that finished recently: a live card keeps its closed members."""
        return _collect_closed(cli, me, now, errors), 0

    def source(read):
        """One source's read with an error list of its own — the sources run at
        once, so they cannot share the producer's — and the merge above walks
        them in source order, so neither the errors nor the view depend on who
        answers first."""
        errors = []
        found, bots = read(errors)
        return found, bots, errors

    sources = [review_requests, my_prs, assigned_issues, jira_tasks, closed_window]
    reads = _each(source, sources)
    errors, items, hidden = [], [], 0
    for found, bots, block_errors in reads:
        items += found
        hidden += bots
        errors += block_errors
    closed = reads[-1][0]          # closed_window: finished work, a lane of its own

    # closed items too: a card that is still live keeps the PR that finished it
    # as a struck child, and only a cluster that is all closed heads with one.
    _with_jira(items + closed, errors, fetch=partial(_jira_issues, cli))
    return build_view(items, hidden_bots=hidden, errors=errors, now=now, closed=closed)


def default_snapshot_path():
    """Where every surface looks by default: next to this script, so the file is
    visible where the queue lives. Override with --json PATH / the plugin's
    config.path."""
    return os.path.join(HERE, "attention.json")


def _state_path(name):
    """User intent, kept beside the snapshot."""
    return os.path.join(HERE, name)


def snooze_path():
    """{key: wake-up ISO}."""
    return _state_path("snoozes.json")


def ack_path():
    """{key: acked-at ISO}. No expiry — an ack holds until the card changes."""
    return _state_path("acks.json")


def config_path():
    """The few knobs that tune one person's dashboard, beside the snapshot like
    parking. Read per request, so an edit lands on the next look; absent means
    the defaults."""
    return _state_path("config.json")


def _read_json(path):
    """Forgiving read: a missing or broken file just means nothing is in it."""
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _atomic_write(path, text):
    """Temp file then rename, so a reader sees the old file or the new one and
    never half of either. `_read_json` treats a torn file as an empty one, which
    on the snapshot costs the previous diff and a whole cold collection, and on
    snoozes and acks costs every entry in them. `mkstemp` gives each writer its
    own temp name: the server and a cron can both write the same file."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=os.path.basename(path) + ".", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _write_json(path, data):
    _atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def load_snoozes(path=None, now=None):
    """{key: until-ISO}, expired entries dropped."""
    stamp = _iso_utc(now or datetime.now(timezone.utc))
    return {k: v for k, v in _read_json(path or snooze_path()).items()
            if isinstance(v, str) and v > stamp}


def load_acks(path=None):
    return {k: v for k, v in _read_json(path or ack_path()).items() if isinstance(v, str)}


# --- sessions and specs: read beside the snapshot, not in it -------------------
# A session is machine state that changes minute to minute and a spec is a
# backlog nobody is waiting on — neither is true of "now" the way a snapshot is,
# and the snapshot is written on the hour. Both are read per request instead, the
# way parking is: the surface asks, the server answers, nothing is stored.

# A pane sitting inside a bb environment belongs to that thread: the path is the
# only join bb and herdr have between them.
BB_ENV = re.compile(r"\.bb/plugins/[^/]+/host-data/(?:worktrees|workspaces)/(thr_[a-z0-9]+)")
# What a focus request may name: a herdr tab, or a bb thread.
FOCUS_TARGET = re.compile(r"^[A-Za-z0-9:_-]{1,64}$")


def _slug_for(path, cli):
    """A checkout -> the container slug the snapshot uses, read from its own
    remote so the join needs no name guessing; the directory name is the
    fallback for a checkout with no origin."""
    try:
        url = cli.text("git", ["-C", path, "remote", "get-url", "origin"]).strip()
    except CliError:
        url = ""
    slug = url.split("github.com", 1)[-1].lstrip(":/").removesuffix(".git") if url else ""
    return slug if "/" in slug else os.path.basename(path.rstrip("/"))


def live_sessions(cli=None):
    """Every agent session open right now, per repo, for a surface to join to its
    cards: herdr panes (including a bare shell sitting in a bb worktree, which
    herdr reports no agent for) and active bb threads. Each one says where it can
    be opened — a herdr tab, a bb thread, or both when the pane is inside bb."""
    cli = cli or LIVE
    repos, slugs, errors = {}, {}, []

    def add(slug, agent, state, label, busy, origin, herdr=None, bb=None, pane=""):
        if slug:
            repos.setdefault(slug, []).append(
                {"agent": agent, "state": state, "label": label, "busy": busy,
                 "origin": origin, "pane": pane, "herdr": herdr, "bb": bb})

    try:
        for pane in cli.json("herdr", ["pane", "list"])["result"]["panes"]:
            path = pane.get("foreground_cwd") or pane.get("cwd") or ""
            thread = BB_ENV.search(path)
            if not pane.get("agent") and not thread:
                continue                      # a plain shell is not a session
            if path not in slugs:
                slugs[path] = _slug_for(path, cli)
            agent = pane.get("agent") or "bb"
            state = pane.get("agent_status", "unknown") if pane.get("agent") else "shell"
            add(slugs[path], agent, state, pane.get("terminal_title_stripped", ""),
                state in ("working", "blocked"), "bb" if thread else "herdr",
                herdr={"tab": pane.get("tab_id", ""), "workspace": pane.get("workspace_id", "")},
                bb={"thread": thread.group(1)} if thread else None,
                pane=pane.get("pane_id", ""))
    except Exception as e:
        if not _absent(e):                # a tool this machine lacks shows no sessions
            errors.append(_error("herdr sessions", e))

    try:
        projects = {p["id"]: p for p in cli.json("bb", ["project", "list", "--json"])}
        for thread in cli.json("bb", ["thread", "list", "--json"]):
            if thread.get("status") != "active":
                continue                      # hundreds of threads; idle is finished
            project = projects.get(thread.get("projectId")) or {}
            path = (project.get("sources") or [{}])[0].get("path", "")
            add(_slug_for(path, cli) if path else "", thread.get("providerId") or "bb", "active",
                thread.get("title") or thread["id"], True, "bb", bb={"thread": thread["id"]})
    except Exception as e:
        if not _absent(e):
            errors.append(_error("bb sessions", e))

    return {"generatedAt": _iso_utc(datetime.now(timezone.utc)), "repos": repos, "errors": errors}


def load_config(path=None):
    """config.json beside the snapshot. Forgiving read: a missing or broken file
    is the defaults, never an error — the same bargain snoozes and acks make."""
    data = _read_json(path or config_path())
    return data if isinstance(data, dict) else {}


def spec_repos(config):
    """`specRepos`: the repos where a `spec`-labelled issue is worth reading
    whoever wrote it. Everywhere else the rail stays mine. A value that is not a
    list of names is none of them, rather than a crash or a wandering search."""
    values = config.get("specRepos")
    return [v for v in values if isinstance(v, str)] if isinstance(values, list) else []


def stalker_on(config):
    """`stalker`: the switch over the whole teammate queue. Absent is on — an
    empty `stalkTeams` is already an off — so only an explicit false closes it,
    and your team list stays where it is while it is closed."""
    return config.get("stalker") is not False


def stalk_teams(config):
    """`stalkTeams`: GitHub team slugs whose members appear under Ongoing work.
    A missing or broken value is nobody, not a search of the whole org."""
    values = config.get("stalkTeams")
    if not isinstance(values, list):
        return []
    return [v.strip() for v in values if isinstance(v, str) and STALK_SLUG.match(v.strip() or "")]


# --- what a card's comments say, read at the time of the look ----------------
# Not in the snapshot, for the same reason sessions and specs are not: a comment
# thread is not a property of "now" the way a card is, and reading one costs a
# call per card. Read per request, cached by (key, lastChangedAt) so a card that
# has not moved since the last look is never read twice, and nothing is stored.
COMMENTS = {}           # key -> (lastChangedAt, [comment])
COMMENT_MAX = 3         # the newest few: all a "something landed" line needs
COMMENT_CHARS = 360     # one line's worth, never a wall
_JIRA_ME = []           # my own account id, once


def adf_text(body):
    """Jira sends a comment body as ADF, not text. Flatten it to the words, with
    the paragraph and list breaks a reader needs and nothing else."""
    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return ""
    out = []

    def walk(node):
        if not isinstance(node, dict):
            return
        kind = node.get("type")
        if kind == "text":
            out.append(node.get("text") or "")
            return
        for child in node.get("content") or []:
            walk(child)
        if kind in ("paragraph", "heading", "listItem", "blockquote", "codeBlock", "rule"):
            out.append("\n")

    walk(body)
    text = "".join(out)
    return " ".join(text.split())


def _jira_me(cli=None):
    """My own Jira account id, so a comment I wrote is not news to me."""
    if _JIRA_ME:
        return _JIRA_ME[0]
    try:
        found = (cli or LIVE).json("twg", ["whoami", "--output", "json", "--output-summary", "none"])
        _JIRA_ME.append(((found.get("data") or {}).get("accountId") or ""))
    except Exception:
        _JIRA_ME.append("")
    return _JIRA_ME[0]


def jira_comments(key, cli=None):
    """One ticket's newest comments, oldest first: who, when, and the words."""
    found = (cli or LIVE).json("twg", ["jira", "workitem", "comment", "query", "--issue-id", key,
                                       "--first", str(COMMENT_MAX), "--order-by=-created",
                                       "--output", "json", "--output-summary", "none"])
    rows = found.get("data") if isinstance(found, dict) else found
    me = _jira_me(cli)
    out = []
    for row in rows if isinstance(rows, list) else []:
        author = row.get("author") or {}
        text = " ".join(adf_text(row.get("body")).split())[:COMMENT_CHARS]
        if not text:
            continue
        out.append({
            "at": _iso_utc(datetime.fromisoformat(_iso(row.get("created")))) if row.get("created") else "",
            "who": author.get("displayName") or "someone",
            "text": text,
            # a support reply written for the customer's eyes, not an internal note
            "internal": row.get("jsdPublic") is False,
            "mine": bool(me) and author.get("accountId") == me,
        })
    return list(reversed(out))          # newest last, the way a thread reads


def card_stamps():
    """key -> when the card last moved, off the snapshot on disk. The stamp is
    what makes the comment read reusable: an unmoved card has the same answer."""
    try:
        data = cached_payload()
    except Exception:
        return {}
    rows = [r for tier in data.get("tiers") or [] for r in tier.get("items") or []]
    rows += (data.get("drafts") or []) + (data.get("closed") or [])
    return {r.get("key") or r.get("ref"): (r.get("lastChangedAt") or "") for r in rows}


def live_comments(keys, cli=None):
    """{key: [comment]} for the cards a surface is about to draw. A key that is
    not a Jira key is dropped: it would be an argument to a CLI, and this is a
    string a page sent. A read that fails costs a line, never the queue."""
    cli = cli or LIVE
    stamps, wanted, out = card_stamps(), [], {}
    for key in keys:
        if not JIRA_KEY.fullmatch(key or ""):
            continue
        stamp = stamps.get(key, "")
        hit = COMMENTS.get(key)
        if hit and stamp and hit[0] == stamp:
            out[key] = hit[1]
        else:
            wanted.append((key, stamp))

    def read(pair):
        key, stamp = pair
        try:
            return key, stamp, jira_comments(key, cli)
        except Exception:
            return key, stamp, None

    for key, stamp, found in _each(read, wanted):
        if found is None:
            continue
        COMMENTS[key] = (stamp, found)
        out[key] = found
    return out


def spec_issues(cli=None, config=None):
    """The proposals I wrote and labelled `spec` that nobody has taken: open and
    **unassigned**. An assignee means someone is already working on it, so it is
    not groundwork any more — and that someone is usually me, which is exactly why
    those tickets are already on the board.

    The repos named in `config.json` are read for anyone's spec instead: a
    teammate's proposal is groundwork I may want to read before it is taken, and
    a queue that only ever shows my own would hide it until someone assigns it.
    Every row carries its `author` and says whether it came from a watched repo,
    so a surface can keep my own backlog apart from what I am watching."""
    cli = cli or LIVE
    config = load_config() if config is None else config
    # Mine first, so a spec both searches return is read as mine.
    searches = [(["search", "issues", "--author=@me", "--label=spec"], "spec issues", False)]
    searches += [(["search", "issues", "--repo", repo, "--label=spec"],
                  f"spec issues ({repo})", True) for repo in spec_repos(config)]
    issues, errors, seen = [], [], set()

    def read(search):
        args, where, watched = search
        try:
            return cli.json("gh", args + ["--state=open", "--limit", "40",
                                            "--json", SPEC_FIELDS]), watched, []
        except Exception as e:
            return [], watched, [_error(where, e)]

    for found, watched, block_errors in _each(read, searches):
        errors += block_errors
        for i in found:
            if i.get("assignees"):
                continue
            ref = f'{i["repository"]["nameWithOwner"]}#{i["number"]}'
            if ref in seen:
                continue
            seen.add(ref)
            issues.append({"repo": i["repository"]["nameWithOwner"], "number": i["number"],
                           "ref": ref, "title": i["title"], "url": i["url"],
                           "updated": _iso(i["updatedAt"]),
                           "author": (i.get("author") or {}).get("login", ""),
                           "watched": watched})
    return {"generatedAt": _iso_utc(datetime.now(timezone.utc)), "issues": issues, "errors": errors}


def focus(request, cli=None):
    """Jump to the session a card's link named — the user's own terminal, on the
    user's own machine. Only an id that looks like one is reachable, because this
    moves the window the reader is looking at: it is a POST, never something a page
    can trigger by being loaded."""
    cli = cli or LIVE
    request = request or {}
    target, kind = request.get("target"), request.get("kind")
    if not isinstance(target, str) or not FOCUS_TARGET.match(target):
        return {"ok": False, "error": "bad target"}
    if kind == "herdr":
        # `tab focus` moves Herdr's own focus to that tab but does not bring the
        # app forward: a reader in a system browser would see nothing happen.
        jump, app = ("herdr", ["tab", "focus", target]), "Herdr"
    elif kind == "bb":
        # bb delivers the thread into the app and likewise leaves its window.
        jump, app = ("bb", ["thread", "open", target]), "bb"
    else:
        return {"ok": False, "error": "unknown kind"}
    ran = [jump[0] + " " + " ".join(jump[1])]
    try:
        cli.text(*jump)
    except Exception as e:
        return {"ok": False, "ran": " && ".join(ran), "error": str(e)[:200]}
    # Raising the window is best effort: the jump already happened, and `open` is
    # not on every platform. The answer still says whether it came forward.
    ran.append("open -a " + app)
    try:
        cli.text("open", ["-a", app])
        raised = True
    except Exception:
        raised = False
    return {"ok": True, "ran": " && ".join(ran), "raised": raised}


def _park(key, value, path, data):
    """Set one card's stamp, or clear it when falsy, then persist."""
    key = (key or "").strip()
    if not key:
        return data
    if value:
        data[key] = value
    else:
        data.pop(key, None)
    _write_json(path, data)
    return data


def write_ack(key, at=None, path=None):
    """Acknowledge one card (`at` defaults to now) or clear it when falsy."""
    path = path or ack_path()
    return _park(key, at, path, load_acks(path))


def write_snooze(key, until=None, path=None, now=None):
    """Sleep one card until `until` (ISO), or wake it when `until` is falsy.
    Expired entries fall out here rather than accumulating forever."""
    path = path or snooze_path()
    return _park(key, until, path, load_snoozes(path, now))


def load_previous(path):
    """Last written snapshot, or None — a warm start for the server after a
    restart, so its first request still diffs against something real."""
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_snapshot(snapshot, path):
    """The one writer: every surface reads this file, refreshes rewrite it."""
    _atomic_write(path, json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")


def collect_payload(previous=None):
    """Live view + changes versus `previous`. Used by --json and /refresh."""
    return payload(collect_view(), previous)


def _collect_closed(cli, me, now, errors):
    """Items closed/merged in the last CLOSED_MEMORY_HOURS, across all sources —
    wider than the lane shows, so a card that is still live keeps its closed
    members. Four independent reads, so they go at once; GraphQL search carries
    the issue/PR links, so each is a single call."""
    since = (now - timedelta(hours=CLOSED_MEMORY_HOURS)).strftime("%Y-%m-%d")
    who = me or "@me"

    def read_prs(qualifier, chip):
        query = ('{ search(type: ISSUE, first: 100, query: "is:pr is:closed %s:%s closed:>=%s") { nodes { '
                 '... on PullRequest { number title url state closedAt headRefName createdAt updatedAt body '
                 'author { login } labels(first:20) { nodes { name color } } repository { nameWithOwner } '
                 'closingIssuesReferences(first:10) { nodes { number repository { nameWithOwner } } } } } } }'
                 ) % (qualifier, who, since)
        try:
            nodes = cli.graphql(query)["data"]["search"]["nodes"]
        except Exception as e:
            return [], [_error(f"closed PRs ({chip})", e)]
        return [_closed_pr_item(n, chip) for n in nodes
                if _within_window(n.get("closedAt"), now, hours=CLOSED_MEMORY_HOURS)], []

    def read_issues():
        query = ('{ search(type: ISSUE, first: 100, query: "is:issue is:closed assignee:%s closed:>=%s") { nodes { '
                 '... on Issue { number title url body closedAt createdAt updatedAt '
                 'labels(first:20) { nodes { name color } } repository { nameWithOwner } '
                 'parent { number repository { nameWithOwner } } '
                 'subIssues(first:50) { nodes { number repository { nameWithOwner } } } '
                 'closedByPullRequestsReferences(first:10) { nodes { number repository { nameWithOwner } } } } } } }'
                 ) % (who, since)
        try:
            nodes = cli.graphql(query)["data"]["search"]["nodes"]
        except Exception as e:
            return [], [_error("closed issues", e)]
        return [_closed_issue_item(n) for n in nodes
                if _within_window(n.get("closedAt"), now, hours=CLOSED_MEMORY_HOURS)], []

    def read_jira():
        try:
            data = cli.json("twg", ["jira", "workitem", "query", "--jql", JIRA_CLOSED_JQL, "--limit", "100",
                                    "--fields", JIRA_CLOSED_FIELDS,
                                    "--output", "json", "--output-summary", "none"])["data"]
        except Exception as e:
            return [], [_error("jira closed", e)]
        return items_from_jira_closed(data["issues"] if isinstance(data, dict) else data), []

    reads = [partial(read_prs, "author", "MY PR"), partial(read_prs, "reviewed-by", "REVIEWED"),
             read_issues, read_jira]
    closed, seen = [], set()
    for found, block_errors in _each(lambda read: read(), reads):
        errors += block_errors
        for item in found:
            # One PR both authored and reviewed by me is one row, and the author
            # read comes first. GitHub numbers issues and PRs in one sequence,
            # so no other pair can collide.
            key = _item_key(item)
            if key in seen:
                continue
            seen.add(key)
            closed.append(item)
    return closed


def _with_jira(items, errors, fetch):
    """Attach the Jira ticket behind each item's link: one query for every key
    the queue names, not one call each. A key that does not resolve is not a
    link: a PR title carries bare KEY-123 tokens (UTF-8 / SHA-256 match that
    shape), and that is a guess, so a guess that misses unlinks without a word."""
    keys = []
    for item in items:
        url = item.get("jira")
        if url:
            key = url.rsplit("/", 1)[-1]
            if key not in keys:
                keys.append(key)

    cache, failure = {}, None
    if keys:
        try:
            cache = fetch(keys)
        except Exception as e:
            failure = e

    reported = set()
    for item in items:
        url = item.get("jira")
        if not url:
            continue
        key = url.rsplit("/", 1)[-1]
        if cache.get(key):
            item["jira_issue"] = cache[key]
        else:
            if item.get("jira_guess"):
                item["jira"] = ""
            elif key not in reported:
                reported.add(key)
                # An absent twg is one error — the source's own — not one per key:
                # the queue's own warnings must not bury what the queue holds.
                exc = failure or CliError("twg jira workitem query", f"no ticket {key} in the answer")
                if not _absent(exc):
                    errors.append(_error(f"jira {key}", exc))
    return items


def _jira_issues(cli, keys):
    """Every linked ticket in one query, with the fields `get` would have
    returned for each. JQL's `key in (…)` quietly leaves out a key that does not
    exist, which is exactly how one wrong guess is supposed to read."""
    data = cli.json("twg", ["jira", "workitem", "query", "--jql", "key in (%s)" % ", ".join(keys),
                            "--limit", "100", "--fields", JIRA_FIELDS,
                            "--output", "json", "--output-summary", "none"])["data"]
    out = {}
    for item in data["issues"] if isinstance(data, dict) else data:
        status = item.get("status") or {}
        out[item["key"]] = {
            "summary": item.get("summary", ""),
            "status": status.get("name", ""),
            "status_category": (status.get("statusCategory") or {}).get("name", ""),
            "type": (item.get("issuetype") or {}).get("name", ""),
        }
    return out


def _whoami(gh):
    try:
        return gh(["api", "user"])["login"]
    except Exception:
        return ""


def _graphql_for(rows, selection, field="pullRequest"):
    """One aliased query reading `selection` from the PR/issue each row names —
    the aliases are `n0`, `n1`, … A per-card read is a process on a network round
    trip, so N of them is N round trips; one query with N aliases is one.
    Returns (query, keys) keyed by `_key(row)`, which is how the answer is handed
    back to the card that asked, so the two cannot be written differently."""
    parts, keys = [], []
    for i, row in enumerate(rows):
        repo, number = row["repository"]["nameWithOwner"], row["number"]
        owner, _, name = repo.partition("/")
        parts.append(f'n{i}: repository(owner:{json.dumps(owner)}, name:{json.dumps(name)}) '
                     f'{{ {field}(number:{int(number)}) {{ {selection} }} }}')
        keys.append(_key(row))
    return "query { " + " ".join(parts) + " }", keys


def _answers(cli, query, keys, field="pullRequest"):
    """{key: node} for a batched answer. A ref the query did not answer for is
    simply absent, the way that card's own read failing used to be."""
    if not keys:
        return {}
    data = (cli.graphql(query) or {}).get("data") or {}
    out = {}
    for i, key in enumerate(keys):
        node = (data.get("n%d" % i) or {}).get(field)
        if node:
            out[key] = node
    return out


# The connections the view model reads as lists, and the checks it reads as
# {name, state} — one selection serves the view, the checks and the threads, so
# one call serves a whole source. `closingIssuesReferences` carries owner and
# name because `_github_links` reads them off the ref.
_PR_SELECTION = (
    "number title isDraft reviewDecision mergeStateStatus state url updatedAt createdAt headRefName body "
    "author { login } labels(first:20) { nodes { name color } } "
    "closingIssuesReferences(first:10) { nodes { number repository { nameWithOwner owner { login } name } } } "
    "statusCheckRollup { contexts(first:100) { nodes { __typename "
    "... on CheckRun { name status conclusion } ... on StatusContext { context state } } } } "
    "reviewThreads(first:50) { nodes { isResolved comments(last:1) { nodes { author { login } body } } } }")
_REVIEW_SELECTION = ("headRefName "
                     "closingIssuesReferences(first:10) "
                     "{ nodes { number repository { nameWithOwner owner { login } name } } }")
_ISSUE_SELECTION = (
    "number title url body updatedAt createdAt labels(first:20) { nodes { name color } } "
    "parent { number state title url } "
    "subIssues(first:50) { nodes { number state title url } } "
    "closedByPullRequestsReferences(first:10) { nodes { number state title url } } "
    # the last hundred: the ball being in your court is always at the end, and
    # the written links a card is built from are in the body or the recent ones.
    "comments(last:100) { nodes { author { login } body } }")


def _checks_of(node):
    """statusCheckRollup -> the {name, state} shape the states already read. A
    check still running reports its status, a finished one its conclusion, which
    is what `gh pr checks` said too."""
    out, rollup = [], node.get("statusCheckRollup") or {}
    for ctx in (rollup.get("contexts") or {}).get("nodes") or []:
        if ctx.get("__typename") == "CheckRun":
            out.append({"name": ctx.get("name", ""),
                        "state": ctx.get("conclusion") or ctx.get("status") or ""})
        else:
            out.append({"name": ctx.get("context", ""), "state": ctx.get("state") or ""})
    return out


def _pr_view(node):
    """A batched pullRequest -> what `gh pr view --json …` gave the view model:
    the connections flattened, everything else as it arrives."""
    view = dict(node)
    view["labels"] = (node.get("labels") or {}).get("nodes") or []
    view["closingIssuesReferences"] = (node.get("closingIssuesReferences") or {}).get("nodes") or []
    return view


def _issue_view(node):
    """A batched issue -> what `gh issue view --json …` gave the view model."""
    return {
        "number": node.get("number"), "title": node.get("title", ""), "url": node.get("url", ""),
        "body": node.get("body"), "updatedAt": node.get("updatedAt"), "createdAt": node.get("createdAt"),
        "labels": (node.get("labels") or {}).get("nodes") or [],
        "comments": [{"author": c.get("author") or {}, "body": c.get("body")}
                     for c in (node.get("comments") or {}).get("nodes") or []],
    }


def _absent(exc):
    """True when the failure is a CLI this machine does not have — a capability
    it never had, not a read that broke. The session readers treat that as
    nothing to show; every other source still says which tool is missing."""
    return isinstance(exc, FileNotFoundError)


def _error(where, exc):
    if isinstance(exc, CliError):
        return {"where": where, "command": exc.command, "output": exc.output}
    entry = {"where": where, "command": "", "output": f"{type(exc).__name__}: {exc}"}
    if _absent(exc):
        # Not a red box: the source is off on this machine, and this names the tool.
        entry["missing"] = exc.filename
    return entry


def team_members(config=None, me=None):
    """Members of `stalkTeams` in config.json under `people` — bots and *me* left
    out, because the reader is the "You" row and a roster that names you twice is
    a roster you stop reading. My own login rides along as `you`, since the avatar
    on that row has to come from somewhere. Not the snapshot: read each request,
    so editing the file changes the rail on the next look."""
    config = load_config() if config is None else config
    if not stalker_on(config):
        return {"you": {}, "people": []}      # no switch, no box, no who-am-I read
    me = me if me is not None else _whoami(partial(LIVE.json, "gh"))
    people = {}
    for slug in stalk_teams(config):
        try:
            members = LIVE.json("gh", ["api", f"orgs/Launchmetrics/teams/{slug}/members"])
        except CliError:
            continue
        for member in members:
            login = member.get("login") or ""
            if not login or login in STALK_BOTS or login == me:
                continue
            person = people.setdefault(login, {"login": login, "name": login, "teams": []})
            if slug not in person["teams"]:
                person["teams"].append(slug)
    def face(login):
        try:
            user = LIVE.json("gh", ["api", f"users/{login}"])
            people[login]["name"] = user.get("name") or login
        except CliError:
            pass
    _each(face, people)
    return {"you": {"login": me},
            "people": sorted(people.values(), key=lambda p: p["name"].lower())}


def jira_account(login):
    """GitHub login -> Jira account id, via the name GitHub publishes.
    Empty when GitHub has no name or Jira has no exact match."""
    try:
        name = (LIVE.json("gh", ["api", f"users/{login}"]).get("name") or "").strip()
    except CliError:
        return ""
    if not name:
        return ""
    try:
        found = LIVE.json("twg", ["user", "search", "--name", name, "--limit", "5",
                                   "--output", "json", "--output-summary", "none"])
    except CliError:
        return ""
    people = found.get("data") if isinstance(found, dict) else found
    if not isinstance(people, list):
        return ""
    exact = [p for p in people if (p.get("name") or "").lower() == name.lower()]
    return ((exact or people)[0].get("accountId") or "") if (exact or people) else ""


def their_queue(login, config=None):
    """One teammate's queue, computed now and not written down. With the
    switch off nothing is read at all."""
    if not stalker_on(load_config() if config is None else config):
        return {"error": "stalker is off"}
    if not STALK_LOGIN.match(login or ""):
        return {"error": "bad login"}
    account = jira_account(login)
    view = collect_view(me=login, cli=AsThem(login, account))
    view["errors"] = [e for e in view.get("errors") or []
                       if "skipped" not in (e.get("output") or "")]
    return payload(view)


# --- HTTP -------------------------------------------------------------------

DASHBOARD = os.path.join(HERE, "dashboard.html")
CONSUMER_JS = os.path.join(HERE, "attention-view.js")
REFERENCE = os.path.join(HERE, "reference.html")


class Server(ThreadingHTTPServer):
    """A reader that goes away mid-request — a page reload, a tab closed, a fetch
    aborted during a refresh — is a dropped connection, not a stack trace on
    the console. Everything else still reports as usual."""

    # One look opens six connections at once (the queue, parking, the stamp and
    # the three rails) and the listen backlog is five, so the rest were dropped:
    # a reset rail read fell back to an empty rail, and a reset queue read left
    # the page on its error box. Measured: 10 at once lost 2-3.
    request_queue_size = 32

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    """Reads are served from the cached snapshot (instant); only /refresh runs
    the producer. Every refresh rewrites the shared file, so file readers (the
    CLI, other tools) and HTTP readers (the page) see the same chain."""

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", ""):
            self.send_file(DASHBOARD, "text/html; charset=utf-8")
        elif path in ("/api/queue", "/attention.json"):
            self.send_json(cached_payload())
        elif path == "/snoozes":
            self.send_json(load_snoozes())
        elif path == "/acks":
            self.send_json(load_acks())
        elif path == "/sessions":
            self.send_json(live_sessions())
        elif path == "/specs":
            self.send_json(spec_issues())
        elif path == "/comments":
            asked = (parse_qs(self.path.split("?", 1)[-1]).get("keys") or [""])[0]
            self.send_json(live_comments([k for k in asked.split(",") if k][:40]))
        elif path == "/status":
            self.send_json(status())
        elif path == "/attention-view.js":
            self.send_file(CONSUMER_JS, "text/javascript; charset=utf-8")
        elif path == "/reference":
            self.send_file(REFERENCE, "text/html; charset=utf-8")
        elif path == "/refresh":
            self.send_json(refresh())
        elif path == "/team":
            self.send_json(team_members())
        elif path == "/queue":
            login = (parse_qs(self.path.split("?", 1)[-1]).get("login") or [""])[0]
            self.send_json(their_queue(login))
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/refresh":
            self.send_json(refresh())
        elif path in ("/snoozes", "/acks"):
            self.send_json(snooze(self.read_body()) if path == "/snoozes" else ack(self.read_body()))
        elif path == "/focus":
            self.send_json(focus(self.read_body()))
        else:
            self.send_error(405)

    def read_body(self):
        try:
            size = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(size) or b"{}")
        except (TypeError, ValueError):
            return {}

    def send_json(self, snapshot):
        self.send_body(json.dumps(snapshot).encode(), "application/json")

    def send_file(self, path, ctype):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError as e:
            body, ctype = f"file not found: {e}".encode(), "text/plain; charset=utf-8"
        self.send_body(body, ctype)

    def send_body(self, body, ctype):
        """One place to write a response."""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


LAST_PAYLOAD = None
LAST_MTIME = None
REFRESH_LOCK = threading.Lock()


def snooze(request):
    """POST /snoozes {key, hours} or {key, until}; hours 0 wakes the card."""
    key = (request or {}).get("key") or ""
    if not key:
        return {"error": "key is required"}
    if request.get("until"):
        until = request["until"]
    else:
        hours = float(request.get("hours") or 0)
        until = _iso_utc(datetime.now(timezone.utc) + timedelta(hours=hours)) if hours else ""
    return write_snooze(key, until)


def ack(request):
    """POST /acks {key} acknowledges; {key, clear: true} puts it back."""
    key = (request or {}).get("key") or ""
    if not key:
        return {"error": "key is required"}
    at = "" if request.get("clear") else (request.get("at") or _iso_utc(datetime.now(timezone.utc)))
    return write_ack(key, at)


def status():
    """What this process is running versus what wrote the snapshot it serves."""
    snapshot = cached_payload()
    mine = {"code": producer_code(), "version": PRODUCER_VERSION}
    theirs = snapshot.get("producer") or {}
    return {"producer": mine,
            "snapshot": {"generatedAt": snapshot.get("generatedAt"), "producer": theirs},
            "stale": theirs.get("code") != mine["code"]}


def cached_payload():
    """What every read gets: the file, the in-memory copy, or one cold-start
    collection when neither exists yet. Another writer (the CLI, another
    collector) can rewrite the file behind us, so a changed mtime wins over the
    cached copy — otherwise this server and the file drift apart silently."""
    global LAST_PAYLOAD, LAST_MTIME
    path = default_snapshot_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if mtime is not None and mtime != LAST_MTIME:
        LAST_PAYLOAD = load_previous(path)
        LAST_MTIME = mtime
    if LAST_PAYLOAD is None:
        LAST_PAYLOAD = refresh()
    return LAST_PAYLOAD


def refresh():
    """Run the producer, write the shared file, keep the chain in memory.
    One at a time: concurrent clicks share nothing but the lock."""
    global LAST_PAYLOAD, LAST_MTIME
    with REFRESH_LOCK:
        # A fresh process has nothing in memory. Diff against the file, or the
        # first refresh after a restart marks every card new and the history
        # of where things were is gone.
        if LAST_PAYLOAD is None:
            LAST_PAYLOAD = load_previous(default_snapshot_path())
        LAST_PAYLOAD = collect_payload(LAST_PAYLOAD)
        path = default_snapshot_path()
        write_snapshot(LAST_PAYLOAD, path)
        try:
            LAST_MTIME = os.path.getmtime(path)
        except OSError:
            LAST_MTIME = None
    return LAST_PAYLOAD


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("port", nargs="?", type=int, default=8765,
                        help="server port (default 8765)")
    parser.add_argument("--json", nargs="?", const="-", metavar="PATH",
                        help="write the shared snapshot JSON and exit; PATH omitted or '-' prints it")
    args = parser.parse_args()
    if args.json is not None:
        path = None if args.json == "-" else args.json
        previous = load_previous(path) if path else None
        snapshot = collect_payload(previous)
        if path:
            write_snapshot(snapshot, path)
            c = snapshot["changes"]["summary"]
            print(f"{path}: {c['new']} new · {c['moved']} moved · {c['gone']} gone")
        else:
            print(json.dumps(snapshot, ensure_ascii=False))
        return
    print(f"Attention queue on http://127.0.0.1:{args.port}  (Ctrl-C to stop)")
    Server(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
