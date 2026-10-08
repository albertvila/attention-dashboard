#!/usr/bin/env python3
"""Attention queue — one queue for GitHub, Jira and starred mail.

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
import html
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

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
PRODUCER_VERSION = 3
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
# Waiting on a support engineer: still waiting on others, but folded out of the
# rendered tier at read time so it does not swell the queue.
SUPPORT_STATUS = "Support Investigating"
JIRA_JQL = ("assignee = currentUser() AND (statusCategory != Done OR "
            + " OR ".join(f'status = "{s}"' for s in DEPLOY_STATUSES)
            + ") ORDER BY updated DESC")
JIRA_FIELDS = ("summary,status,issuetype,description,updated,created,project,assignee,issuelinks")
# statuscategorychangeddate = when the ticket entered the Done category.
JIRA_CLOSED_FIELDS = JIRA_FIELDS + ",statuscategorychangeddate"
# The inward text of the link type that holds a ticket up: the linked issue is
# the blocker, and Jira hands back its status and summary with the link itself.
JIRA_BLOCKED_BY = "is blocked by"
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
# Mail: the star is the gate — every starred thread shows up, archived ones
# included, and un-starring is how it leaves. A `group/<name>` label on several
# threads merges them into one card; see the dashboard help section.
MAIL_ACCOUNT = os.environ.get("MAIL_ACCOUNT", "you@launchmetrics.com")
MAIL_QUERY = os.environ.get("MAIL_QUERY", "is:starred -in:trash")
MAIL_MAX = 50
MAIL_GROUP_PREFIX = "group/"

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
# `title` rides along because a child without a ref (mail) has nothing else to
# render as its link text.
CHILD_FIELDS = ("chip", "key", "ref", "title", "url", "states", "age", "detail", "facts", "labels", "times")


def _closed_updated(group):
    """The newest closure stamp in a cluster: `times.updated` is the closure for
    every closed source, and a cluster is as fresh as its freshest member."""
    return max((i.get("times", {}).get("updated") or "" for i in group), default="")


def build_view(items, hidden_bots=0, errors=None, now=None, closed=None):
    """Normalized items -> tiered, ordered view model. No I/O, no gh.
    Linked items (issue/PR refs, Jira keys) become one card: the most urgent
    member that is still live first, the rest as compact children. Open and
    closed items cluster together, so a merged PR stays with its ticket — as a
    struck child, never the header: finished work does not name a card that
    still has live work. The cluster's best state still decides the tier, so a
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
        # Live members first, so a merged PR rides struck under the ticket it
        # belongs to instead of naming the card from the finished side.
        rows.sort(key=lambda r: (_row_state(r) in CLOSED_STATES, r["_rank"],
                                 r["draft"], r["_updated"]))
        # The tier is the cluster's best state, not the header's. A merged PR is
        # the evidence that the live member's own state is behind reality — a
        # ticket still In Review, an issue nobody replied to — so the card lands
        # where the finished member points, next to the follow-up (close the
        # ticket, deploy it), instead of buried among everything that waits.
        lands = min(rows, key=lambda r: r["_rank"])
        # the header's own Jira ticket is already shown inline; don't repeat it.
        jira_key = rows[0]["jira"].rsplit("/", 1)[-1] if rows[0]["jira"] else ""
        kids = [r for r in rows[1:] if r["ref"] != jira_key]
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
    closed_rows.sort(key=lambda r: r["_updated"], reverse=True)
    for r in closed_rows:
        del r["_updated"]
        del r["_rank"]
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
    return {
        "chip": item.get("chip", ""),
        "key": _item_key(item),
        "title": item.get("title", ""),
        "ref": item.get("ref", ""),
        "url": item.get("url", ""),
        "author": item.get("author", ""),
        "jira": item.get("jira", ""),
        "links": item.get("links") or [],   # what clustered this card; empty = on its own
        "blocked_by": item.get("blocked_by") or [],   # what holds it up, in its own line
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
        "_updated": _utc(item.get("times", {}).get("updated")),
        "_rank": [t for t, _ in TIERS].index(tier),
    }


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


def _move_label(was_row, row):
    """Human summary of a move: the state change when there is one, the tier
    change otherwise (a second state — conflicts — can move a row alone).
    A section-only move (a merged card leaving the closed log) has neither, and
    must not read as 'Ready when you are → Ready when you are'."""
    from_label, to_label = _row_state_label(was_row), _row_state_label(row)
    if from_label != to_label:
        return f"{from_label} → {to_label}"
    from_tier, to_tier = _row_tier(was_row), _row_tier(row)
    if from_tier != to_tier:
        titles = dict(TIERS)
        return f"{titles.get(from_tier, from_tier)} → {titles.get(to_tier, to_tier)}"
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
                    (_row_state(row), _row_tier(row), section):
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
          jira="", links=None, blocked_by=(), states=(), detail="", facts=(), labels=(), times=None,
          draft=False):
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
        "states": list(states),
        "detail": detail,
        "facts": list(facts or []),
        "labels": list(labels or []),
        "times": dict(times or {}),
        "draft": draft,
    }
    if key:
        item["key"] = key
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
    return _item(
        "REVIEW", container=repo, ref=_ref(repo, row["number"]), title=row["title"], url=row["url"],
        author=(row.get("author") or {}).get("login", ""), jira=jira_url(row.get("headRefName")),
        links=_github_links(row.get("closingIssuesReferences")) + _issue_refs_in(row.get("title"), repo),
        labels=_labels(row.get("labels")), states=["review-requested"], detail="review requested",
        times={"updated": row.get("updatedAt"), "created": row.get("createdAt")},
        draft=bool(row.get("isDraft")))


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
    return _item(
        "MY PR", container=repo, ref=_ref(repo, view["number"]), title=view["title"],
        url=view["url"], author=(view.get("author") or {}).get("login", ""),
        jira=jira_url(view.get("headRefName")) or _jira_url_in(_text_of(view)),
        links=list(dict.fromkeys(_github_links(view.get("closingIssuesReferences"))
                                 + _issue_refs_in(view.get("title"), repo)
                                 + _github_links_in(_text_of(view)))),
        labels=_labels(view.get("labels")), facts=_pr_facts(view, checks), states=states,
        detail=_pr_detail(view, states, checks, threads),
        times={"updated": view.get("updatedAt"), "created": view.get("createdAt")},
        draft=bool(view.get("isDraft")))


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
    links += _github_links_in(_text_of(view))
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


def _jira_item(row, *, states, detail="", facts=(), blocked_by=(), updated=None):
    """A Jira row -> the item every Jira card shares. The two callers differ only
    in what they say about the ticket: its states, its detail line, its facts, and
    which stamp counts as `updated`."""
    desc = row.get("description")
    key = row["key"]
    return _item(
        "JIRA", source="jira", ref=key, title=row.get("summary", ""),
        url=row.get("url") or JIRA_BASE + key,
        links=_github_links_in(desc if isinstance(desc, str) else json.dumps(desc)),
        blocked_by=blocked_by, states=states, detail=detail, facts=facts,
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


def mail_url(account, thread_id):
    """The URL gmcli's own `url` command prints, built without a second call."""
    return f"https://mail.google.com/mail/?authuser={quote(account)}#all/{thread_id}"


def _epoch_ms(ms):
    """Gmail's internalDate is epoch milliseconds."""
    try:
        return _iso_utc(datetime.fromtimestamp(int(ms) / 1000, timezone.utc))
    except (TypeError, ValueError, OSError):
        return ""


def _mail_groups(errors, run):
    """`group/<name>` user labels -> {label id: label name}. gmcli's labels
    list prints a table (no --json), so this is the one parsed source; a failure
    only costs grouping. `run` is the adapter's text call."""
    try:
        out = run("gmcli", [MAIL_ACCOUNT, "labels", "list"])
    except Exception as e:
        errors.append(_error("mail groups", e))
        return {}
    groups = {}
    for line in out.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].strip() == "user" \
                and parts[1].strip().lower().startswith(MAIL_GROUP_PREFIX):
            groups[parts[0].strip()] = parts[1].strip()
    return groups


def items_from_mail(threads, groups=None):
    """gmcli search --json -> one card per thread. Threads sharing a
    `group/<name>` label merge (their name rides along as a fact); a Jira key or
    GitHub ref in the subject/snippet merges the thread into that card."""
    items = []
    for thread in threads or []:
        messages = sorted(thread.get("messages") or [], key=lambda m: m.get("internalDate") or 0)
        if not messages:
            continue
        first, last = messages[0], messages[-1]
        thread_id = thread.get("id") or last.get("threadId") or ""
        label_ids = {lid for m in messages for lid in (m.get("labelIds") or [])}
        group = next((groups[lid] for lid in sorted(label_ids) if groups and lid in groups), "")
        text = f'{last.get("subject", "")} {last.get("snippet", "")}'
        keys = JIRA_KEY.findall(text)
        facts = []
        if "UNREAD" in label_ids:
            facts.append({"label": "unread", "tone": "warn"})
        if len(messages) > 1:
            facts.append({"label": f"{len(messages)} messages", "tone": "quiet"})
        if any(m.get("hasAttachments") for m in messages):
            facts.append({"label": "attachment", "tone": "quiet"})
        if group:
            facts.append({"label": group, "tone": "info"})
        sender_name, sender_address = parseaddr(last.get("from", ""))
        items.append(_item(
            "MAIL", source="mail", key=f"mail/{thread_id}", title=last.get("subject", ""),
            url=mail_url(MAIL_ACCOUNT, thread_id),
            author=sender_name or last.get("from", ""),
            jira=jira_url(keys[-1]) if keys else "",
            links=([group] if group else []) + _github_links_in(text),
            # the badge follows who sent last: the mailbox account means the ball
            # is with them, anyone else (or no sender at all) means it is yours.
            states=["waiting-reply" if sender_address == MAIL_ACCOUNT else "needs-reply"],
            detail=html.unescape(last.get("snippet", ""))[:140],
            facts=facts,
            times={"updated": _epoch_ms(last.get("internalDate")),
                   "created": _epoch_ms(first.get("internalDate"))}))
    return items


def _closed_pr_item(node, chip):
    repo = node["repository"]["nameWithOwner"]
    return _item(
        chip, container=repo, ref=_ref(repo, node["number"]), title=node["title"], url=node["url"],
        author=(node.get("author") or {}).get("login", ""), jira=jira_url(node.get("headRefName")),
        links=_graphql_links((node.get("closingIssuesReferences") or {}).get("nodes"))
              + _issue_refs_in(node.get("title"), repo),
        labels=_labels((node.get("labels") or {}).get("nodes")),
        states=["merged" if node.get("state") == "MERGED" else "closed"],
        times={"updated": node.get("closedAt"), "created": node.get("createdAt")})


def _closed_issue_item(node):
    repo = node["repository"]["nameWithOwner"]
    links = _graphql_links([node["parent"]] if node.get("parent") else [])
    for group in ("subIssues", "closedByPullRequestsReferences"):
        links += _graphql_links((node.get(group) or {}).get("nodes"))
    return _item("ISSUE", container=repo, ref=_ref(repo, node["number"]), title=node["title"],
                 url=node["url"], links=links, jira=_jira_url_in(node.get("body")),
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


class Cli:
    """The one seam every CLI call crosses: text, JSON and GraphQL alike.
    Production uses LIVE; a test injects a recorded adapter, so a whole
    collection can run without touching the live tools."""

    def text(self, command, args):
        proc = subprocess.run([command] + args, capture_output=True, text=True)
        if proc.returncode != 0:
            raise CliError(command + " " + " ".join(args), (proc.stderr or proc.stdout).strip())
        return proc.stdout

    def json(self, command, args):
        return json.loads(self.text(command, args))

    def graphql(self, query):
        return self.json("gh", ["api", "graphql", "-f", "query=" + query])


LIVE = Cli()


def collect_view(me=None, cli=None):
    """Run the live queries and build the view model. Read-only."""
    cli = cli or LIVE
    errors, items, hidden = [], [], 0
    now = datetime.now(timezone.utc)
    # The per-card helpers take gh JSON in argv shape; bind that call once off
    # the adapter so they need no second parameter.
    gh = partial(cli.json, "gh")
    me = me or _whoami(gh)

    try:
        # user-review-requested, not review-requested: the latter also matches
        # team requests (a team you're in was asked), which is not your queue.
        rows = gh(["search", "prs", f"user-review-requested:{me or '@me'}", "--state=open",
                   "--json", PR_FIELDS, "--limit", "100"])
        for row in rows:
            try:
                info = gh(["pr", "view", str(row["number"]),
                           "--repo", row["repository"]["nameWithOwner"],
                           "--json", "headRefName,closingIssuesReferences"])
                row["headRefName"] = info.get("headRefName")
                row["closingIssuesReferences"] = info.get("closingIssuesReferences")
            except Exception as e:
                errors.append(_error(_key(row), e))
        got, hidden = items_from_review_search(rows)
        items += got
    except Exception as e:
        errors.append(_error("review requests", e))

    try:
        rows = gh(["search", "prs", "--author=@me", "--state=open", "--json", PR_FIELDS, "--limit", "100"])
        views, checks, threads = {}, {}, {}
        for row in rows:
            key = _key(row)
            repo, num = row["repository"]["nameWithOwner"], row["number"]
            try:
                views[key] = gh(["pr", "view", str(num), "--repo", repo, "--json",
                                 "number,title,isDraft,reviewDecision,mergeStateStatus,state,url,updatedAt,createdAt,"
                                 "headRefName,author,labels,closingIssuesReferences,body"])
                checks[key] = _pr_checks(gh, repo, num)
                threads[key] = _review_threads(cli, repo, num)
            except Exception as e:
                errors.append(_error(f"{_ref(repo, num)}", e))
        items += items_from_own_prs(rows, views, checks, threads, me)
    except Exception as e:
        errors.append(_error("my PRs", e))

    try:
        rows = gh(["search", "issues", "--assignee=@me", "--state=open", "--json", ISSUE_FIELDS, "--limit", "100"])
        views, linked = {}, {}
        for row in rows:
            key = _key(row)
            repo, num = row["repository"]["nameWithOwner"], row["number"]
            try:
                views[key] = gh(["issue", "view", str(num), "--repo", repo, "--json",
                                 "number,title,url,body,updatedAt,createdAt,comments,labels"])
                linked[key] = _issue_links(cli, repo, num)
            except Exception as e:
                errors.append(_error(f"{_ref(repo, num)}", e))
        items += items_from_issues(rows, views, linked, me)
    except Exception as e:
        errors.append(_error("assigned issues", e))

    try:
        data = cli.json("twg", ["jira", "workitem", "query", "--jql", JIRA_JQL, "--limit", "100",
                                "--fields", JIRA_FIELDS,
                                "--output", "json", "--output-summary", "none"])["data"]
        items += items_from_jira(data["issues"] if isinstance(data, dict) else data)
    except Exception as e:
        errors.append(_error("jira tasks", e))

    try:
        threads = cli.json("gmcli", [MAIL_ACCOUNT, "search", MAIL_QUERY,
                                      "--max", str(MAIL_MAX), "--json"]).get("threads") or []
        items += items_from_mail(threads, _mail_groups(errors, cli.text))
    except Exception as e:
        errors.append(_error("mail", e))

    closed = _collect_closed(cli, me, now, errors)

    # closed items too: a card that is still live keeps the PR that finished it
    # as a struck child, and only a cluster that is all closed heads with one.
    _with_jira(items + closed, errors, fetch=partial(_jira_issue, cli))
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


def _write_json(path, data):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


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
    for args, where, watched in searches:
        try:
            found = cli.json("gh", args + ["--state=open", "--limit", "40",
                                            "--json", SPEC_FIELDS])
        except Exception as e:
            errors.append(_error(where, e))
            continue
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
    user's own machine. Three commands are reachable and only for an id that looks
    like one, because this moves the window the reader is looking at: it is a
    POST, never something a page can trigger by being loaded."""
    cli = cli or LIVE
    request = request or {}
    target, kind = request.get("target"), request.get("kind")
    if not isinstance(target, str) or not FOCUS_TARGET.match(target):
        return {"ok": False, "error": "bad target"}
    if kind == "herdr":
        # `tab focus` switches Herdr's own window to the pane: it raises itself.
        commands = [("herdr", ["tab", "focus", target])]
    elif kind == "bb":
        # bb delivers the thread into the app but leaves its window where it was,
        # so the jump raises it too — otherwise nothing appears to happen.
        commands = [("bb", ["thread", "open", target]), ("open", ["-a", "bb"])]
    else:
        return {"ok": False, "error": "unknown kind"}
    ran = [c + " " + " ".join(a) for c, a in commands]
    try:
        for command, args in commands:
            cli.text(command, args)
    except Exception as e:
        return {"ok": False, "ran": " && ".join(ran), "error": str(e)[:200]}
    return {"ok": True, "ran": " && ".join(ran)}


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
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(snapshot, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def collect_payload(previous=None):
    """Live view + changes versus `previous`. Used by --json and /refresh."""
    return payload(collect_view(), previous)


def _collect_closed(cli, me, now, errors):
    """Items closed/merged in the last CLOSED_MEMORY_HOURS, across all sources —
    wider than the lane shows, so a card that is still live keeps its closed
    members. GraphQL search: one call per source, and it carries the issue/PR links."""
    since = (now - timedelta(hours=CLOSED_MEMORY_HOURS)).strftime("%Y-%m-%d")
    who = me or "@me"
    closed, seen = [], set()
    for qualifier, chip in (("author", "MY PR"), ("reviewed-by", "REVIEWED")):
        query = ('{ search(type: ISSUE, first: 100, query: "is:pr is:closed %s:%s closed:>=%s") { nodes { '
                 '... on PullRequest { number title url state closedAt headRefName createdAt updatedAt '
                 'author { login } labels(first:20) { nodes { name color } } repository { nameWithOwner } '
                 'closingIssuesReferences(first:10) { nodes { number repository { nameWithOwner } } } } } } }'
                 ) % (qualifier, who, since)
        try:
            nodes = cli.graphql(query)["data"]["search"]["nodes"]
        except Exception as e:
            errors.append(_error(f"closed PRs ({chip})", e))
            continue
        for node in nodes:
            key = f'{node["repository"]["nameWithOwner"]}#{node["number"]}'
            if key in seen or not _within_window(node.get("closedAt"), now, hours=CLOSED_MEMORY_HOURS):
                continue
            seen.add(key)
            closed.append(_closed_pr_item(node, chip))
    query = ('{ search(type: ISSUE, first: 100, query: "is:issue is:closed assignee:%s closed:>=%s") { nodes { '
             '... on Issue { number title url body closedAt createdAt updatedAt '
             'labels(first:20) { nodes { name color } } repository { nameWithOwner } '
             'parent { number repository { nameWithOwner } } '
             'subIssues(first:50) { nodes { number repository { nameWithOwner } } } '
             'closedByPullRequestsReferences(first:10) { nodes { number repository { nameWithOwner } } } } } } }'
             ) % (who, since)
    try:
        nodes = cli.graphql(query)["data"]["search"]["nodes"]
        closed += [_closed_issue_item(n) for n in nodes if _within_window(n.get("closedAt"), now, hours=CLOSED_MEMORY_HOURS)]
    except Exception as e:
        errors.append(_error("closed issues", e))
    try:
        data = cli.json("twg", ["jira", "workitem", "query", "--jql", JIRA_CLOSED_JQL, "--limit", "100",
                                "--fields", JIRA_CLOSED_FIELDS,
                                "--output", "json", "--output-summary", "none"])["data"]
        closed += items_from_jira_closed(data["issues"] if isinstance(data, dict) else data)
    except Exception as e:
        errors.append(_error("jira closed", e))
    return closed


def _with_jira(items, errors, fetch):
    """Attach the Jira ticket behind each item's link; one twg call per key.
    A key that does not resolve is not a link: mail subjects and snippets carry
    bare KEY-123 tokens, and UTF-8 / SHA-256 match that shape."""
    cache, failures, reported = {}, {}, set()
    for item in items:
        url = item.get("jira")
        if not url:
            continue
        key = url.rsplit("/", 1)[-1]
        if key not in cache and key not in failures:
            try:
                cache[key] = fetch(key)
            except Exception as e:
                failures[key] = e
        if cache.get(key):
            item["jira_issue"] = cache[key]
        elif key in failures:
            if item.get("source") == "mail":
                item["jira"] = ""
            elif key not in reported:
                reported.add(key)
                # An absent twg is one error — the source's own — not one per key:
                # the queue's own warnings must not bury what the queue holds.
                if not _absent(failures[key]):
                    errors.append(_error(f"jira {key}", failures[key]))
    return items


def _jira_issue(cli, key):
    """twg jira workitem get -> the few fields the dashboard shows."""
    item = cli.json("twg", ["jira", "workitem", "get", key, "--output", "json",
                            "--output-summary", "none"])["data"][0]
    status = item.get("status") or {}
    return {
        "summary": item.get("summary", ""),
        "status": status.get("name", ""),
        "status_category": (status.get("statusCategory") or {}).get("name", ""),
        "type": (item.get("issuetype") or {}).get("name", ""),
    }


def _whoami(gh):
    try:
        return gh(["api", "user"])["login"]
    except Exception:
        return ""


def _pr_checks(gh, repo, num):
    """gh pr checks exits 1 on 'no checks reported' — a normal state, not an error."""
    try:
        return gh(["pr", "checks", str(num), "--repo", repo, "--json", "name,state"])
    except CliError as e:
        if "no checks reported" in e.output:
            return []
        raise


def _review_threads(cli, repo, num):
    owner, name = repo.split("/")
    query = (f'query {{ repository(owner:"{owner}", name:"{name}") {{ pullRequest(number:{num}) {{ '
             'reviewThreads(first:50) { nodes { isResolved comments(last:1) { nodes { author { login } body } } } }'
             ' } } }')
    return cli.graphql(query)["data"]["repository"]["pullRequest"]


def _issue_links(cli, repo, num):
    owner, name = repo.split("/")
    query = (f'query {{ repository(owner:"{owner}", name:"{name}") {{ issue(number:{num}) {{ '
             'closedByPullRequestsReferences(first:10) { nodes { number state title url } } '
             'parent { number state title url } '
             'subIssues(first:50) { nodes { number state title url } }'
             ' } } }')
    return cli.graphql(query)["data"]["repository"]["issue"]


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


# --- HTTP -------------------------------------------------------------------

DASHBOARD = os.path.join(HERE, "dashboard.html")
CONSUMER_JS = os.path.join(HERE, "attention-view.js")
REFERENCE = os.path.join(HERE, "reference.html")


class Server(ThreadingHTTPServer):
    """A reader that goes away mid-request — a page reload, a tab closed, a fetch
    aborted during a ~20s refresh — is a dropped connection, not a stack trace on
    the console. Everything else still reports as usual."""

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
        elif path == "/status":
            self.send_json(status())
        elif path == "/attention-view.js":
            self.send_file(CONSUMER_JS, "text/javascript; charset=utf-8")
        elif path == "/reference":
            self.send_file(REFERENCE, "text/html; charset=utf-8")
        elif path == "/refresh":
            self.send_json(refresh())
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
