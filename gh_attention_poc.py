#!/usr/bin/env python3
"""GitHub attention POC — one attention queue for GitHub signals.

Run:  python3 gh_attention_poc.py     then open http://127.0.0.1:8765
      python3 gh_attention_poc.py --json attention.json   one shared snapshot

Standard library only, read-only (search/view/checks/GraphQL reads, never a
mutating call). Sources are adapters that emit normalized items
{source, container, states, detail, times}; classification into tiers and
ordering is source-agnostic so Jira/Things/etc. can arrive later as adapters.

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

# --- settled vocabulary -----------------------------------------------------

NEEDS, READY, WAITING = "needs", "ready", "waiting"
TIERS = [(NEEDS, "Needs you now"), (READY, "Ready when you are"), (WAITING, "Waiting on others")]

# Source-agnostic: a state maps to a tier, nothing else. First tier in TIERS
# order that any of an item's states maps to wins (needs > ready > waiting).
STATE_TIER = {
    "review-requested": NEEDS,
    "needs-comments": NEEDS,
    "ci-failing": NEEDS,
    "conflicts": NEEDS,
    "needs-reply": NEEDS,
    "not-started": NEEDS,
    "ready": READY,
    "to-deploy": READY,
    "merged": READY,
    "done": READY,
    "waiting": WAITING,
    "waiting-reply": WAITING,
    "in-progress": WAITING,
    "closed": WAITING,
}

STATE_LABEL = {
    "review-requested": "review",
    "needs-comments": "needs comments",
    "ci-failing": "CI failing",
    "conflicts": "merge conflicts",
    "ready": "ready",
    "to-deploy": "to deploy",
    "waiting": "waiting",
    "needs-reply": "needs reply",
    "waiting-reply": "waiting reply",
    "in-progress": "in progress",
    "not-started": "not started",
    "merged": "merged",
    "done": "done",
    "closed": "closed",
}

# Shared snapshot contract: bump when a field changes meaning or disappears.
SCHEMA_VERSION = 1
# Bumped by hand for human-meaningful changes; the code hash below is the
# automatic "which build wrote this file" witness a stale process is caught by.
PRODUCER_VERSION = 2
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

# Renderer-agnostic chip tone per state; each surface picks its own colors.
STATE_TONE = {
    "review-requested": "info",
    "needs-comments": "warn",
    "ci-failing": "bad",
    "conflicts": "bad",
    "needs-reply": "warn",
    "ready": "good",
    "merged": "good",
    "done": "good",
    "in-progress": "info",
    "to-deploy": "info",
    "reviewed": "info",
    "waiting": "quiet",
    "waiting-reply": "quiet",
    "not-started": "quiet",
    "closed": "quiet",
}

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

JIRA_BASE = "https://launchmetrics.atlassian.net/browse/"
JIRA_KEY = re.compile(r"[A-Z][A-Z0-9]+-\d+")
# Done-category statuses that still need you (deploy work that isn't finished).
DEPLOY_STATUSES = ("TO_DEPLOY",)
JIRA_JQL = ("assignee = currentUser() AND (statusCategory != Done OR "
            + " OR ".join(f'status = "{s}"' for s in DEPLOY_STATUSES)
            + ") ORDER BY updated DESC")
JIRA_FIELDS = "summary,status,issuetype,description,updated,created,project,assignee"
# statuscategorychangeddate = when the ticket entered the Done category.
JIRA_CLOSED_FIELDS = JIRA_FIELDS + ",statuscategorychangeddate"
CLOSED_WINDOW_HOURS = 24
# Mail: the star is the gate — every starred thread shows up, archived ones
# included, and un-starring is how it leaves. A `group/<name>` label on several
# threads merges them into one card; see the dashboard help section.
MAIL_ACCOUNT = os.environ.get("MAIL_ACCOUNT", "albert.vila@launchmetrics.com")
MAIL_QUERY = os.environ.get("MAIL_QUERY", "is:starred -in:trash")
MAIL_MAX = 50
MAIL_GROUP_PREFIX = "group/"

JIRA_CLOSED_JQL = ("assignee = currentUser() AND statusCategory = Done "
                   "AND statuscategorychangeddate >= -1d "
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


def build_view(items, hidden_bots=0, errors=None, now=None, closed=None):
    """Normalized items -> tiered, ordered view model. No I/O, no gh.
    Linked items (issue/PR refs, Jira keys) become one card: most urgent
    member first, the rest as compact children. Open and closed items cluster
    together, so a merged PR stays with its ticket and lifts the card to the
    merged state's tier (Ready) instead of splitting into the closed log; a
    cluster whose members are all closed goes to the closed log."""
    now = now or datetime.now(timezone.utc)
    closed_items = list(closed or [])
    closed_ids = {id(item) for item in closed_items}   # no marker on the caller's dicts
    buckets = {key: [] for key, _ in TIERS}
    drafts = []
    closed_rows = []
    for group in _clusters(list(items) + closed_items):
        rows = [_row(item, now) for item in group]
        rows.sort(key=lambda r: (r["_rank"], r["draft"], r["_updated"]))
        # the header's own Jira ticket is already shown inline; don't repeat it.
        jira_key = rows[0]["jira"].rsplit("/", 1)[-1] if rows[0]["jira"] else ""
        kids = [r for r in rows[1:] if r["ref"] != jira_key]
        rows[0]["children"] = [{k: r[k] for k in CHILD_FIELDS} for r in kids]
        if all(id(item) in closed_ids for item in group):
            closed_rows.append(rows[0])
        else:
            # drafts are tests/POCs: out of the attention tiers, into their own section.
            (drafts if rows[0]["draft"] else buckets[rows[0]["tier"]]).append(rows[0])
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
    tiers = [STATE_TIER.get(s) for s in states]
    tier = next((t for t, _ in TIERS if t in tiers), WAITING)
    return {
        "chip": item.get("chip", ""),
        "key": _item_key(item),
        "title": item.get("title", ""),
        "ref": item.get("ref", ""),
        "url": item.get("url", ""),
        "author": item.get("author", ""),
        "jira": item.get("jira", ""),
        "links": item.get("links") or [],   # what clustered this card; empty = on its own
        "jira_issue": item.get("jira_issue"),
        "container": item.get("container", ""),
        "labels": item.get("labels") or [],
        "facts": item.get("facts") or [],
        "states": [
            {"key": s, "label": STATE_LABEL.get(s, s), "tier": STATE_TIER.get(s, WAITING),
             "tone": STATE_TONE.get(s, "quiet")}
            for s in states
        ],
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
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()


def humanize_age(iso, now):
    if not iso:
        return ""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    secs = max(0, (now - dt).total_seconds())
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
    """(section, row) for every card a surface renders, children included."""
    for tier in view.get("tiers") or []:
        for row in tier["items"]:
            yield tier["key"], row
            for child in row.get("children") or []:
                yield tier["key"], child
    for row in view.get("drafts") or []:
        yield "drafts", row
    for row in view.get("closed") or []:
        yield "closed", row


# Gone rows stay in the snapshot long enough for a surface that was not open
# when they vanished; consumers filter by their own last-look timestamp.
GONE_WINDOW_HOURS = 24 * 7
MAX_GONE = 100


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
        gone.update({"section": section, "goneAt": stamp, "change": {"kind": "gone"}})
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
    return {
        "source": "github",
        "chip": "REVIEW",
        "container": repo,
        "ref": _ref(repo, row["number"]),
        "title": row["title"],
        "url": row["url"],
        "author": (row.get("author") or {}).get("login", ""),
        "jira": jira_url(row.get("headRefName")),
        "links": _github_links(row.get("closingIssuesReferences")) + _issue_refs_in(row.get("title"), repo),
        "labels": _labels(row.get("labels")),
        "states": ["review-requested"],
        "detail": f"review requested",
        "times": {"updated": row.get("updatedAt"), "created": row.get("createdAt")},
        "draft": bool(row.get("isDraft")),
    }


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
    detail = _pr_detail(view, states, checks, threads)
    return {
        "source": "github",
        "chip": "MY PR",
        "container": repo,
        "ref": _ref(repo, view["number"]),
        "title": view["title"],
        "url": view["url"],
        "author": (view.get("author") or {}).get("login", ""),
        "jira": jira_url(view.get("headRefName")) or _jira_url_in(_text_of(view)),
        "links": list(dict.fromkeys(_github_links(view.get("closingIssuesReferences"))
                                    + _issue_refs_in(view.get("title"), repo)
                                    + _github_links_in(_text_of(view)))),
        "labels": _labels(view.get("labels")),
        "facts": _pr_facts(view, checks),
        "states": states,
        "detail": detail,
        "times": {"updated": view.get("updatedAt"), "created": view.get("createdAt")},
        "draft": bool(view.get("isDraft")),
    }


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
    return {
        "source": "github",
        "chip": "ISSUE",
        "container": repo,
        "ref": _ref(repo, view["number"]),
        "title": view["title"],
        "url": view["url"],
        "links": list(dict.fromkeys(links)),
        "jira": _jira_url_in(_text_of(view)),
        "states": states,
        "detail": " · ".join(parts),
        "labels": _labels(view.get("labels")),
        "times": {"updated": view.get("updatedAt"), "created": view.get("createdAt")},
        "draft": False,
    }


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
        desc = row.get("description")
        key = row["key"]
        if status_name in DEPLOY_STATUSES:
            state = "to-deploy"
        elif category == "In Progress":
            state = "in-progress"
        else:
            state = "not-started"
        tone = "warn" if status_name in DEPLOY_STATUSES else \
               {"Done": "ok", "In Progress": "warn"}.get(category, "waiting")
        items.append({
            "source": "jira",
            "chip": "JIRA",
            "container": "",
            "ref": key,
            "title": row.get("summary", ""),
            "url": row.get("url") or JIRA_BASE + key,
            "author": "",
            "jira": "",
            "links": _github_links_in(desc if isinstance(desc, str) else json.dumps(desc)),
            "states": [state],
            "detail": itype.get("name", ""),
            "facts": [{"label": status_name, "tone": tone}] if status_name else [],
            "labels": [],
            "times": {"updated": _iso(row.get("updated")), "created": _iso(row.get("created"))},
            "draft": False,
        })
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


def _mail_groups(errors, run=None):
    """`group/<name>` user labels -> {label id: label name}. gmcli's labels
    list prints a table (no --json), so this is the one parsed source; a failure
    only costs grouping."""
    try:
        out = (run or run_text)("gmcli", [MAIL_ACCOUNT, "labels", "list"])
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
        items.append({
            "source": "mail",
            "chip": "MAIL",
            "key": f"mail/{thread_id}",
            "container": "",
            "ref": "",
            "title": last.get("subject", ""),
            "url": mail_url(MAIL_ACCOUNT, thread_id),
            "author": parseaddr(last.get("from", ""))[0] or last.get("from", ""),
            "jira": jira_url(keys[-1]) if keys else "",
            "links": ([group] if group else []) + _github_links_in(text),
            "states": ["needs-reply"],
            "detail": html.unescape(last.get("snippet", ""))[:140],
            "facts": facts,
            "labels": [],
            "times": {"updated": _epoch_ms(last.get("internalDate")),
                      "created": _epoch_ms(first.get("internalDate"))},
            "draft": False,
        })
    return items


def _closed_pr_item(node, chip):
    repo = node["repository"]["nameWithOwner"]
    return {
        "source": "github",
        "chip": chip,
        "container": repo,
        "ref": _ref(repo, node["number"]),
        "title": node["title"],
        "url": node["url"],
        "author": (node.get("author") or {}).get("login", ""),
        "jira": jira_url(node.get("headRefName")),
        "links": _graphql_links((node.get("closingIssuesReferences") or {}).get("nodes"))
                 + _issue_refs_in(node.get("title"), repo),
        "labels": _labels((node.get("labels") or {}).get("nodes")),
        "states": ["merged" if node.get("state") == "MERGED" else "closed"],
        "detail": "",
        "times": {"updated": node.get("closedAt"), "created": node.get("createdAt")},
        "draft": False,
    }


def _closed_issue_item(node):
    repo = node["repository"]["nameWithOwner"]
    links = _graphql_links([node["parent"]] if node.get("parent") else [])
    for group in ("subIssues", "closedByPullRequestsReferences"):
        links += _graphql_links((node.get(group) or {}).get("nodes"))
    return {
        "source": "github",
        "chip": "ISSUE",
        "container": repo,
        "ref": _ref(repo, node["number"]),
        "title": node["title"],
        "url": node["url"],
        "author": "",
        "links": links,
        "jira": _jira_url_in(node.get("body")),
        "labels": _labels((node.get("labels") or {}).get("nodes")),
        "states": ["closed"],
        "detail": "",
        "times": {"updated": node.get("closedAt"), "created": node.get("createdAt")},
        "draft": False,
    }


def items_from_jira_closed(rows):
    """Tickets that entered the Done category recently (deploy statuses excluded)."""
    items = []
    for row in rows:
        status = row.get("status") or {}
        itype = row.get("issueType") or row.get("issuetype") or {}
        desc = row.get("description")
        key = row["key"]
        items.append({
            "source": "jira",
            "chip": "JIRA",
            "container": "",
            "ref": key,
            "title": row.get("summary", ""),
            "url": row.get("url") or JIRA_BASE + key,
            "author": "",
            "links": _github_links_in(desc if isinstance(desc, str) else json.dumps(desc)),
            "labels": [],
            "states": ["done"],
            "detail": " · ".join(p for p in (status.get("name", ""), itype.get("name", "")) if p),
            "times": {"updated": _iso(row.get("statuscategorychangeddate") or row.get("updated")),
                      "created": _iso(row.get("created"))},
            "draft": False,
        })
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


def _within_window(ts, now, hours=CLOSED_WINDOW_HOURS):
    """True when ts is inside the last `hours` — date-filtered searches over-fetch."""
    if not ts:
        return False
    dt = datetime.fromisoformat(_iso(ts).replace("Z", "+00:00"))
    return (now - dt).total_seconds() <= hours * 3600


def _key(row):
    return f'{row["repository"]["nameWithOwner"]}#{row["number"]}'


# --- live collection: CLI subprocesses ---------------------------------------

class CliError(RuntimeError):
    def __init__(self, command, output):
        self.command = command
        self.output = output
        super().__init__(output)


def run_text(command, args):
    proc = subprocess.run([command] + args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise CliError(command + " " + " ".join(args), (proc.stderr or proc.stdout).strip())
    return proc.stdout


def run_json(command, args):
    return json.loads(run_text(command, args))


def run_gh(args):
    return run_json("gh", args)


def run_graphql(query):
    return run_gh(["api", "graphql", "-f", "query=" + query])


def collect_view(me=None, gh=run_gh):
    """Run the live queries and build the view model. Read-only."""
    errors, items, hidden = [], [], 0
    now = datetime.now(timezone.utc)
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
                threads[key] = _review_threads(gh, repo, num)
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
                linked[key] = _issue_links(gh, repo, num)
            except Exception as e:
                errors.append(_error(f"{_ref(repo, num)}", e))
        items += items_from_issues(rows, views, linked, me)
    except Exception as e:
        errors.append(_error("assigned issues", e))

    try:
        data = run_json("twg", ["jira", "workitem", "query", "--jql", JIRA_JQL, "--limit", "100",
                                "--fields", JIRA_FIELDS,
                                "--output", "json", "--output-summary", "none"])["data"]
        items += items_from_jira(data["issues"] if isinstance(data, dict) else data)
    except Exception as e:
        errors.append(_error("jira tasks", e))

    try:
        threads = run_json("gmcli", [MAIL_ACCOUNT, "search", MAIL_QUERY,
                                      "--max", str(MAIL_MAX), "--json"]).get("threads") or []
        items += items_from_mail(threads, _mail_groups(errors))
    except Exception as e:
        errors.append(_error("mail", e))

    closed = _collect_closed(gh, me, now, errors)

    # closed items too: a merged PR can become the header of its card, and the
    # ticket it names is shown inline there rather than as a child.
    _with_jira(items + closed, errors)
    return build_view(items, hidden_bots=hidden, errors=errors, now=now, closed=closed)


def default_snapshot_path():
    """Where every surface looks by default: next to this script, so the file is
    visible where the POC lives. Override with --json PATH / the plugin's
    config.path."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "attention.json")


def snooze_path():
    """User intent, kept beside the snapshot: {key: wake-up ISO}."""
    return os.path.join(os.path.dirname(os.path.abspath(default_snapshot_path())), "snoozes.json")


def load_snoozes(path=None, now=None):
    """{key: until-ISO}, expired entries dropped. Read-only and forgiving: a
    missing or broken file just means nothing is snoozed."""
    now = now or datetime.now(timezone.utc)
    try:
        with open(path or snooze_path()) as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    stamp = _iso_utc(now)
    return {k: v for k, v in raw.items() if isinstance(v, str) and v > stamp}


def ack_path():
    """Acknowledgements live beside snoozes: {key: acked-at ISO}. No expiry — an
    ack holds until the card itself changes."""
    return os.path.join(os.path.dirname(os.path.abspath(default_snapshot_path())), "acks.json")


def load_acks(path=None):
    try:
        with open(path or ack_path()) as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, str)}


def write_ack(key, at=None, path=None):
    """Acknowledge one card (`at` defaults to now) or clear it when falsy."""
    path = path or ack_path()
    data = load_acks(path)
    key = (key or "").strip()
    if not key:
        return data
    if at:
        data[key] = at
    else:
        data.pop(key, None)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return data


def write_snooze(key, until=None, path=None, now=None):
    """Sleep one card until `until` (ISO), or wake it when `until` is falsy.
    Expired entries fall out here rather than accumulating forever."""
    path = path or snooze_path()
    data = load_snoozes(path, now)
    key = (key or "").strip()
    if not key:
        return data
    if until:
        data[key] = until
    else:
        data.pop(key, None)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return data


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


def _collect_closed(gh, me, now, errors):
    """Items closed/merged in the last CLOSED_WINDOW_HOURS, across all sources.
    GraphQL search: one call per source, and it carries the issue/PR links."""
    since = (now - timedelta(hours=CLOSED_WINDOW_HOURS)).strftime("%Y-%m-%d")
    who = me or "@me"
    closed, seen = [], set()
    for qualifier, chip in (("author", "MY PR"), ("reviewed-by", "REVIEWED")):
        query = ('{ search(type: ISSUE, first: 100, query: "is:pr is:closed %s:%s closed:>=%s") { nodes { '
                 '... on PullRequest { number title url state closedAt headRefName createdAt updatedAt '
                 'author { login } labels(first:20) { nodes { name color } } repository { nameWithOwner } '
                 'closingIssuesReferences(first:10) { nodes { number repository { nameWithOwner } } } } } } }'
                 ) % (qualifier, who, since)
        try:
            nodes = run_graphql(query)["data"]["search"]["nodes"]
        except Exception as e:
            errors.append(_error(f"closed PRs ({chip})", e))
            continue
        for node in nodes:
            key = f'{node["repository"]["nameWithOwner"]}#{node["number"]}'
            if key in seen or not _within_window(node.get("closedAt"), now):
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
        nodes = run_graphql(query)["data"]["search"]["nodes"]
        closed += [_closed_issue_item(n) for n in nodes if _within_window(n.get("closedAt"), now)]
    except Exception as e:
        errors.append(_error("closed issues", e))
    try:
        data = run_json("twg", ["jira", "workitem", "query", "--jql", JIRA_CLOSED_JQL, "--limit", "100",
                                "--fields", JIRA_CLOSED_FIELDS,
                                "--output", "json", "--output-summary", "none"])["data"]
        closed += items_from_jira_closed(data["issues"] if isinstance(data, dict) else data)
    except Exception as e:
        errors.append(_error("jira closed", e))
    return closed


def _with_jira(items, errors, fetch=None):
    """Attach the Jira ticket behind each item's link; one twg call per key.
    A key that does not resolve is not a link: mail subjects and snippets carry
    bare KEY-123 tokens, and UTF-8 / SHA-256 match that shape."""
    fetch = fetch or _jira_issue
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
                errors.append(_error(f"jira {key}", failures[key]))
    return items


def _jira_issue(key):
    """twg jira workitem get -> the few fields the dashboard shows."""
    item = run_json("twg", ["jira", "workitem", "get", key, "--output", "json",
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


def _review_threads(gh, repo, num):
    owner, name = repo.split("/")
    query = (f'query {{ repository(owner:"{owner}", name:"{name}") {{ pullRequest(number:{num}) {{ '
             'reviewThreads(first:50) { nodes { isResolved comments(last:1) { nodes { author { login } body } } } }'
             ' } } }')
    return run_graphql(query)["data"]["repository"]["pullRequest"]


def _issue_links(gh, repo, num):
    owner, name = repo.split("/")
    query = (f'query {{ repository(owner:"{owner}", name:"{name}") {{ issue(number:{num}) {{ '
             'closedByPullRequestsReferences(first:10) { nodes { number state title url } } '
             'parent { number state title url } '
             'subIssues(first:50) { nodes { number state title url } }'
             ' } } }')
    return run_graphql(query)["data"]["repository"]["issue"]


def _error(where, exc):
    if isinstance(exc, CliError):
        return {"where": where, "command": exc.command, "output": exc.output}
    return {"where": where, "command": "", "output": f"{type(exc).__name__}: {exc}"}


# --- HTTP -------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>GitHub attention</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{color-scheme:light dark}
body{font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;background:#0d1117;color:#e6edf3}
header{position:sticky;top:0;background:#161b22;border-bottom:1px solid #30363d;padding:12px 20px;display:flex;gap:12px;align-items:baseline}
h1{font-size:15px;margin:0;font-weight:600}
button{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:4px 12px;cursor:pointer;font:inherit}
button:hover{background:#30363d}
#stamp{color:#8b949e;font-size:12px}
main{max-width:940px;margin:0 auto;padding:16px 20px 60px}
section{margin:22px 0}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:#8b949e;margin:0 0 8px}
.row{display:flex;gap:10px;align-items:flex-start;padding:9px 12px;border:1px solid #30363d;border-radius:8px;background:#161b22;margin-bottom:6px}
.chip{flex:0 0 auto;font-size:10px;font-weight:700;letter-spacing:.05em;padding:2px 6px;border-radius:4px;background:#30363d;color:#c9d1d9}
.chip.REVIEW{background:#1f6feb33;color:#79c0ff}
.chip.REVIEWED{background:#58a6ff22;color:#79c0ff}
.chip.MY{background:#23863633;color:#7ee787}
.chip.ISSUE{background:#d2992233;color:#e3b341}
.chip.JIRA{background:#8957e533;color:#d2a8ff}
.chip.MAIL{background:#39c5cf22;color:#76e3ea}
/* Always visible (a hover-only control is undiscoverable), but quiet until the
   row is hovered. */
.snooze{flex:0 0 auto}
.snooze select,.snooze button{font:inherit;font-size:11px;background:transparent;color:#6e7681;border:1px solid transparent;border-radius:6px;padding:1px 6px;cursor:pointer}
.row:hover .snooze select,.row:hover .snooze button{background:#21262d;color:#c9d1d9;border-color:#30363d}
.wakeat{color:#6e7681;font-size:11px;margin-left:4px}
.row.snoozed{opacity:.5}
.help{margin:0;padding:0 0 0 18px;color:#8b949e;font-size:12.5px;max-width:940px}
.help li{margin:5px 0}
.body{flex:1;min-width:0}
.title{font-weight:600}
.title a{color:#e6edf3;text-decoration:none}
.title a:hover{text-decoration:underline}
.meta{color:#8b949e;font-size:12px;margin-top:2px;overflow-wrap:anywhere}
.avatar{width:16px;height:16px;border-radius:50%;vertical-align:-3px;margin-right:4px;background:#30363d}
.author,.repo{color:#8b949e;text-decoration:none}
.author:hover,.repo:hover{color:#e6edf3;text-decoration:underline}
.jira{color:#79c0ff;text-decoration:none}
.jira:hover{text-decoration:underline}
.jira-line{margin-top:4px;font-size:12px;overflow-wrap:anywhere}
.jira-type{color:#8b949e;margin-left:6px;font-size:11px}
.jira-summary{color:#c9d1d9;margin-left:6px}
.badge.wip{background:#d299221f;color:#e3b341;border-color:#d2992255}
.labels{margin-top:4px}
.fact{display:inline-block;font-size:10px;font-weight:600;padding:1px 7px;border-radius:10px;border:1px solid;margin-left:4px;vertical-align:1px}
.fact.ok{color:#7ee787;border-color:#3fb95055;background:#3fb95011}
.fact.bad{color:#ff7b72;border-color:#f8514955;background:#f8514911}
.fact.warn{color:#e3b341;border-color:#d2992255;background:#d2992211}
.fact.waiting{color:#8b949e;border-color:#8b949e55;background:#8b949e11}
.gh-label{display:inline-block;font-size:10px;font-weight:600;padding:1px 7px;border-radius:10px;margin:0 4px 2px 0;border:1px solid #00000033}
.children{margin-top:6px;border-left:2px solid #30363d;padding-left:10px}
.child{display:flex;align-items:center;gap:6px;padding:2px 0;font-size:12px;flex-wrap:wrap}
.child a{color:#8b949e;text-decoration:none}
.child a:hover{color:#e6edf3;text-decoration:underline}
.child .gh-label{font-size:9px;padding:0 6px}
.child .fact{font-size:9px;padding:0 6px}
.child-detail{color:#8b949e;font-size:11px}
.chip.mini{font-size:9px;padding:1px 5px}
.child-age{margin-left:auto;color:#6e7681;font-size:11px}
.badge{display:inline-block;font-size:11px;padding:1px 7px;border-radius:10px;margin-left:6px;border:1px solid transparent}
.badge.needs{background:#f851491f;color:#ff7b72;border-color:#f8514955}
.badge.ready{background:#3fb9501f;color:#7ee787;border-color:#3fb95055}
.badge.waiting{background:#8b949e1f;color:#8b949e;border-color:#8b949e55}
.badge.draft{background:#8b949e26;color:#c9d1d9;border-color:#8b949e88;text-transform:uppercase;letter-spacing:.06em;font-weight:700}
.row.draft{border-style:dashed;opacity:.72}
.row.draft:hover{opacity:1}
/* Change is a row-level, transient fact: a coloured edge plus plain meta text,
   never another chip next to the item's own states and facts. */
.row.chg-new{border-left:3px solid #3fb950}
.row.chg-moved{border-left:3px solid #d29922}
.row.chg-moved.out{border-left-color:#3fb950}
.chg.moved.out{color:#7ee787}
.row.chg-gone{border-left:3px solid #6e7681;opacity:.5}
.row.chg-gone .title a{text-decoration:line-through}
.chg{font-weight:600}
.chg.new{color:#7ee787}
.chg.moved{color:#e3b341}
.chg.moved.out{color:#7ee787}
.chg.gone{color:#8b949e}
details.fold{margin:0 0 8px}
details.fold summary{cursor:pointer;font-size:12px;color:#8b949e;padding:6px 10px;border:1px dashed #30363d;border-radius:8px;background:#161b22;list-style-position:inside}
details.fold[open] summary{margin-bottom:6px}
.age{flex:0 0 auto;color:#8b949e;font-size:12px}
.empty{color:#6e7681;font-size:13px;padding:4px 2px}
.hidden{color:#8b949e;font-size:12px;margin-top:6px}
.err{border:1px solid #f85149;border-radius:8px;background:#f8514911;padding:10px 12px;margin-bottom:8px}
.err b{color:#ff7b72}
pre{white-space:pre-wrap;margin:6px 0 0;font-size:12px;color:#c9d1d9;max-height:180px;overflow:auto}
</style></head><body>
<header><h1>GitHub attention</h1><button id="refresh">Refresh</button><span id="stamp">loading…</span></header>
<main id="app"></main>
<script src="attention-view.js"></script>
<script>
const app = document.getElementById('app');
const stamp = document.getElementById('stamp');
/* The rules live in attention-view.js, shared with the prototype and the dsh
   panel: this page only renders what the overlay hands it. */
const SEEN_KEY = 'gha.seenAt';
let seenAt = null;
try { seenAt = localStorage.getItem(SEEN_KEY); } catch(e) {}
let snoozes = {}, acks = {}, status = null, view = null;

async function load(isRefresh){
  stamp.textContent = isRefresh ? 'refreshing — live gh/twg collection, ~20s…' : 'loading…';
  try{
    const res = await fetch(isRefresh ? '/refresh' : '/api/queue', {cache:'no-store'});
    const data = await res.json();
    const [snaps, acksRes, statusRes] = await Promise.all([
      fetch('/snoozes', {cache:'no-store'}), fetch('/acks', {cache:'no-store'}), fetch('/status', {cache:'no-store'})]);
    snoozes = await snaps.json().catch(() => ({}));
    acks = await acksRes.json().catch(() => ({}));
    status = await statusRes.json().catch(() => null);
    view = AttentionView.overlay(data, {seenAt, snoozes, acks});
    render(data);
    stamp.textContent = 'snapshot ' + ageOf(data.generatedAt) + ' · ' + view.summaryText();
    try { localStorage.setItem(SEEN_KEY, data.generatedAt); } catch(e) {}
  }catch(e){
    app.innerHTML = '';
    app.appendChild(errBox({where:'page', command:'GET /api/queue', output:String(e)}));
    stamp.textContent = 'error';
  }
}
function render(data){
  app.innerHTML = '';
  const staleText = AttentionView.staleText(data, status);
  if(staleText) app.appendChild(el('div','err', staleText));
  for(const fold of [['Snoozed', view.folds.snoozed, 'wake one to bring it back'],
                     ['Acknowledged', view.folds.acked, 'hidden until the card changes'],
                     ['Drafts', view.folds.drafts, 'tests / POCs — collapsed by default'],
                     ['Recently closed', view.folds.closed, 'last 24h']]){
    if(fold[1].length) app.appendChild(foldBox(fold[0], fold[1], fold[2]));
  }
  for(const tier of view.tiers){
    const s = document.createElement('section');
    const h = document.createElement('h2');
    h.textContent = tier.title + ' · ' + tier.count;
    s.appendChild(h);
    if(!tier.rows.length){ s.appendChild(el('div','empty','Nothing here.')); }
    for(const it of tier.rows) s.appendChild(row(it));
    app.appendChild(s);
  }
  if(view.folds.mail.length) app.appendChild(foldBox('Mail', view.folds.mail, 'starred threads — collapsed by default'));
  for(const e of data.errors) app.appendChild(errBox(e));
  if(data.hidden_bots) app.appendChild(el('div','hidden','+'+data.hidden_bots+' hidden (bot-authored review requests)'));
  app.appendChild(helpBox([
    'Mail — every starred thread shows up, archived ones included (is:starred), collected in the collapsed Mail fold instead of the tiers. Un-star one to clear it; nothing here changes your mail.',
    'Tiers — Needs you now: the ball is yours (review requests, failing checks, unresolved threads, replies owed, unstarted work). Ready when you are: nothing blocks you (approved and green PRs, merged work whose ticket may still need closing). Waiting on others: you handed off (PRs awaiting review, tickets in progress or with support, mail you answered).',
    'Grouping — two ways: a Jira key or GitHub ref links items into one card automatically (a merged PR stays with its ticket and lifts the card to Ready), and a group/<name> label merges threads that share nothing but the topic. The top line of a card is its most urgent member; the rest are children.',
    'Change marks — a coloured edge and small text mean "since your last look": new, moved (A → B), or dropped (struck through, and hidden in Needs you now).',
    'Parking — the picker on each card snoozes it (4 hours / 1 day / 3 days / 1 week) or acknowledges it (until it changes). Shared with the other dashboards; the snapshot itself is untouched.'
  ]));
}
function foldBox(title, items, note){
  const d = document.createElement('details');
  d.className = 'fold';
  const s = document.createElement('summary');
  s.textContent = title + ' · ' + items.length + (note ? ' (' + note + ')' : '');
  d.appendChild(s);
  for(const it of items) d.appendChild(row(it));
  return d;
}
async function park(key, value){
  const acking = value === 'ack', unacking = value === 'unack';
  const res = await fetch(acking || unacking ? '/acks' : '/snoozes', {
    method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify(acking ? {key} : unacking ? {key, clear: true} : {key, hours: Number(value)})});
  const stored = await res.json();
  if(acking || unacking) acks = stored; else snoozes = stored;
  load();
}
function parkControl(r){
  const wrap = el('span','snooze');
  const snoozeUntil = view.snoozeOf(r), ackedAt = view.ackOf(r);
  if(snoozeUntil || ackedAt){
    const b = el('button','wake', ackedAt ? 'unack' : 'wake');
    b.onclick = () => park(r.key, ackedAt ? 'unack' : '0');
    wrap.appendChild(b);
    wrap.appendChild(el('span','wakeat', ackedAt ? 'until it changes' : view.formatWhen(snoozeUntil)));
    return wrap;
  }
  const sel = document.createElement('select');
  const ph = document.createElement('option');
  ph.value = ''; ph.textContent = '⏰ snooze / ack';
  sel.appendChild(ph);
  for(const c of view.choices){
    const o = document.createElement('option');
    o.value = c.value; o.textContent = c.label;
    sel.appendChild(o);
  }
  sel.onchange = () => { if(sel.value) park(r.key, sel.value); };
  wrap.appendChild(sel);
  return wrap;
}
const ageOf = iso => {
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  return s < 60 ? 'just now' : s < 3600 ? Math.floor(s/60) + 'm ago'
       : s < 86400 ? Math.floor(s/3600) + 'h ago' : Math.floor(s/86400) + 'd ago';
};
function chipClass(c){return c==='REVIEW'?'REVIEW':c==='ISSUE'?'ISSUE':c==='JIRA'?'JIRA':c==='MAIL'?'MAIL':c==='REVIEWED'?'REVIEWED':'MY';}
function row(it){
  const chg = view.flag(it);
  const r = el('div','row' + (it.draft ? ' draft' : '') + view.changeClass(chg)
                     + (view.parked(it) ? ' snoozed' : ''));
  const chip = el('span','chip '+chipClass(it.chip), it.chip);
  r.appendChild(chip);
  const b = el('div','body');
  const t = el('div','title');
  const a = document.createElement('a');
  a.href = it.url; a.target = '_blank'; a.rel='noreferrer'; a.textContent = it.title;
  t.appendChild(a);
  for(const st of it.states){ const bg = el('span','badge '+st.tier, st.label); t.appendChild(bg); }
  if(it.draft) t.appendChild(el('span','badge draft','draft'));
  b.appendChild(t);
  const m = el('div','meta');
  if(chg){
    m.appendChild(el('span','chg ' + view.changeTone(chg), view.changeLabel(chg)));
    m.appendChild(document.createTextNode(' · '));
  }
  if(it.author){
    const img = document.createElement('img');
    img.className='avatar'; img.loading='lazy'; img.alt=''; img.title='by '+it.author;
    img.src='https://github.com/'+encodeURIComponent(it.author)+'.png?size=32';
    m.appendChild(img);
    const who = document.createElement('a');
    who.className='author'; who.target='_blank'; who.rel='noreferrer'; who.textContent=it.author;
    who.href='https://github.com/'+encodeURIComponent(it.author);
    m.appendChild(who);
    m.appendChild(document.createTextNode(' · '));
  }
  const refParts = it.ref.split('#');
  if(it.container){
    const repo = document.createElement('a');
    repo.className='repo'; repo.target='_blank'; repo.rel='noreferrer';
    repo.href='https://github.com/'+it.container; repo.textContent=refParts[0];
    m.appendChild(repo);
    m.appendChild(document.createTextNode('#'+refParts[1]));
  }else{
    m.appendChild(document.createTextNode(it.ref));
  }
  for(const f of (it.facts||[])) m.appendChild(el('span','fact '+(f.tone||'warn'), f.label));
  if(it.detail) m.appendChild(document.createTextNode(' · ' + it.detail));
  b.appendChild(m);
  if(it.jira){
    const jr = el('div','jira-line');
    const j = document.createElement('a');
    j.className='jira'; j.href=it.jira; j.target='_blank'; j.rel='noreferrer';
    j.textContent=it.jira.split('/').pop();
    jr.appendChild(j);
    if(it.jira_issue){
      if(it.jira_issue.type) jr.appendChild(el('span','jira-type', it.jira_issue.type));
      if(it.jira_issue.status){
        const cat = (it.jira_issue.status_category||'').toLowerCase();
        const t = cat==='done'?'ready':cat==='in progress'?'wip':'waiting';
        jr.appendChild(el('span','badge '+t, it.jira_issue.status));
      }
      if(it.jira_issue.summary) jr.appendChild(el('span','jira-summary', it.jira_issue.summary));
    }
    b.appendChild(jr);
  }
  if(it.labels && it.labels.length){
    const ls = el('div','labels');
    for(const l of it.labels) ls.appendChild(labelChip(l));
    b.appendChild(ls);
  }
  if(it.children && it.children.length){
    const kids = el('div','children');
    for(const c of it.children){
      const k = el('div','child');
      k.appendChild(el('span','chip mini '+chipClass(c.chip), c.chip));
      const a = document.createElement('a');
      a.href=c.url; a.target='_blank'; a.rel='noreferrer';
      a.textContent = c.ref || c.title || '';
      a.title = c.title || c.ref || '';
      k.appendChild(a);
      for(const st of c.states) k.appendChild(el('span','badge '+st.tier, st.label));
      const cchg = view.flag(c);
      if(cchg) k.appendChild(el('span','chg ' + view.changeTone(cchg), view.changeLabel(cchg)));
      for(const f of (c.facts||[])) k.appendChild(el('span','fact '+(f.tone||'warn'), f.label));
      for(const l of (c.labels||[])) k.appendChild(labelChip(l));
      if(c.detail) k.appendChild(el('span','child-detail', c.detail));
      if(c.age) k.appendChild(el('span','child-age', c.age));
      kids.appendChild(k);
    }
    b.appendChild(kids);
  }
  r.appendChild(b);
  r.appendChild(el('div','age', it.age));
  r.appendChild(parkControl(it));
  return r;
}
function helpBox(lines){
  const d = document.createElement('details');
  d.className = 'fold';
  const s = document.createElement('summary');
  s.textContent = 'How this queue works';
  d.appendChild(s);
  const ul = document.createElement('ul');
  ul.className = 'help';
  for(const line of lines) ul.appendChild(el('li', null, line));
  d.appendChild(ul);
  return d;
}
function labelChip(l){
  const hex = (l.color||'').replace('#','').padEnd(6,'0');
  const r=parseInt(hex.slice(0,2),16)||0, g=parseInt(hex.slice(2,4),16)||0, bl=parseInt(hex.slice(4,6),16)||0;
  const s = el('span','gh-label',l.name);
  s.style.background='#'+hex;
  s.style.color=(0.299*r+0.587*g+0.114*bl)>150?'#24292f':'#ffffff';
  return s;
}
function errBox(e){
  const d = el('div','err');
  d.appendChild(el('div',null,e.where+': a gh call failed'));
  if(e.command) d.appendChild(el('pre',null,e.command));
  if(e.output) d.appendChild(el('pre',null,e.output));
  return d;
}
function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!=null)n.textContent=text;return n;}
document.getElementById('refresh').onclick = () => load(true);
load();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    """Reads are served from the cached snapshot (instant); only /refresh runs
    the producer. Every refresh rewrites the shared file, so file readers (the
    dsh plugin) and HTTP readers (pages, other tools) see the same chain."""

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/api/queue", "/attention.json"):
            self.send_json(cached_payload())
        elif path == "/snoozes":
            self.send_snoozes()
        elif path == "/acks":
            self.send_json(load_acks())
        elif path == "/status":
            self.send_json(status())
        elif path == "/attention-view.js":
            self.send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "attention-view.js"), "text/javascript; charset=utf-8")
        elif path == "/refresh":
            self.send_json(refresh())
        elif path.startswith("/prototype"):
            self.send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "prototype-attention-dashboard.html"), "text/html; charset=utf-8")
        else:
            self.send_html(PAGE)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/refresh":
            self.send_json(refresh())
        elif path in ("/snoozes", "/acks"):
            self.send_json(snooze(self.read_body()) if path == "/snoozes" else ack(self.read_body()))
        else:
            self.send_response(405)
            self.end_headers()

    def read_body(self):
        try:
            size = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(size) or b"{}")
        except (TypeError, ValueError):
            return {}

    def send_snoozes(self):
        self.send_json(load_snoozes())

    def send_json(self, snapshot):
        body = json.dumps(snapshot).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError as e:
            body = f"file not found: {e}".encode()
            ctype = "text/plain; charset=utf-8"
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
    collection when neither exists yet. Another writer (the CLI, the panel's
    refresh) can rewrite the file behind us, so a changed mtime wins over the
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
            with open(path, "w") as fh:
                json.dump(snapshot, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            c = snapshot["changes"]["summary"]
            print(f"{path}: {c['new']} new · {c['moved']} moved · {c['gone']} gone")
        else:
            print(json.dumps(snapshot, ensure_ascii=False))
        return
    print(f"GitHub attention POC on http://127.0.0.1:{args.port}  (Ctrl-C to stop)")
    print(f"  page       http://127.0.0.1:{args.port}/")
    print(f"  prototype  http://127.0.0.1:{args.port}/prototype")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
