#!/usr/bin/env python3
"""Fixture-driven test of the producer's one CLI seam.

Run:  python3 -m unittest -v test_cli_seam

One recording drives a whole collection: every answer is keyed by the exact
command recorded, so a per-card lookup on the wrong key raises instead of
quietly answering nothing, and an unrecorded command can never read as an empty
result. The closed-window searches are the one exception: the producer computes
their `closed:>=` date from its own clock mid-run, so they key on the search
they ask for with that date normalized (see key) — every other part of the
search still has to match. GitHub answers come from fixtures/gh_output.json; the
twg (Jira) and gmcli (mail) answers are built here, since no live recording is
committed.
"""

import json
import re
import unittest
from pathlib import Path

import attention

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "gh_output.json").read_text())
ME = FIXTURES["me"]

# The wire shapes the fixture was recorded from; a command the producer issues
# with different arguments is a different recorded command.
PR_SEARCH = "number,title,repository,author,createdAt,updatedAt,isDraft,url,labels"
ISSUE_SEARCH = "number,title,repository,createdAt,updatedAt,url,commentsCount,labels"
PR_VIEW = ("number,title,isDraft,reviewDecision,mergeStateStatus,state,url,updatedAt,createdAt,"
           "headRefName,author,labels,closingIssuesReferences,body")
ISSUE_VIEW = "number,title,url,body,updatedAt,createdAt,comments,labels"

# A GraphQL `search(type: ISSUE)` query keys on the search it asks for, with its
# `closed:>=` date normalized — the producer reads that date from its own clock.
SEARCH = re.compile(r'search\(type: ISSUE.*?query: "([^"]*)"')
CLOSED_DATE = re.compile(r"closed:>=\d{4}-\d{2}-\d{2}")
CLOSED_ANY = "closed:>=<date>"


def key(command, args):
    """The recording's key for one invocation: exact args, except a search
    query keys on its normalized search alone, so a different search still
    raises."""
    keys = []
    for arg in args:
        search = SEARCH.search(arg)
        keys.append(CLOSED_DATE.sub(CLOSED_ANY, search.group(1)) if search else arg)
    return (command, tuple(keys))


class Recorded(attention.Cli):
    """The recorded adapter: text, JSON and GraphQL answer a recorded command
    and raise for anything else, so a fake cannot pass by answering nothing.
    Its recording is the shape the live capture shim writes, so one recording
    replays through it unchanged."""

    def __init__(self, calls):
        self.answers = {key(c["command"][0], c["command"][1:]): c for c in calls}

    def text(self, command, args):
        call = self.answers.get(key(command, args))
        if call is None:
            raise LookupError(f"unrecorded command: {command} {' '.join(args)}")
        if call["returncode"] != 0:
            raise attention.CliError(command + " " + " ".join(args),
                                     call["stdout"].strip())
        return call["stdout"]


def answer(command, args, payload, code=0):
    """One recorded invocation; payload is the stdout the tool recorded."""
    return {"command": [command] + list(args), "returncode": code,
            "stdout": payload if isinstance(payload, str) else json.dumps(payload)}


def gh(args, payload):
    return answer("gh", args, payload)


def review_threads_query(owner, name, num):
    return (f'query {{ repository(owner:"{owner}", name:"{name}") {{ pullRequest(number:{num}) {{ '
            'reviewThreads(first:50) { nodes { isResolved comments(last:1) { nodes { author { login } body } } } }'
            ' } } }')


def issue_links_query(owner, name, num):
    return (f'query {{ repository(owner:"{owner}", name:"{name}") {{ issue(number:{num}) {{ '
            'closedByPullRequestsReferences(first:10) { nodes { number state title url } } '
            'parent { number state title url } '
            'subIssues(first:50) { nodes { number state title url } }'
            ' } } }')


def jira_row(key, summary, status="In Progress", category="In Progress"):
    """One twg workitem row, with the fields the producer reads."""
    return {"key": key, "summary": summary,
            "status": {"name": status, "statusCategory": {"name": category}},
            "issuetype": {"name": "Task"}, "description": None,
            "updated": "2026-09-28T10:00:00.000+0200", "created": "2026-09-18T10:00:00.000+0200",
            "url": attention.JIRA_BASE + key}


JIRA_OPEN = [jira_row("RBT-700", "Waiting on a reviewer"),
             jira_row("FIRE-90000", "Not started yet", status="To Do", category="To Do")]

MAIL_THREAD = {"id": "t-seam", "messages": [
    {"id": "m-seam", "threadId": "t-seam", "labelIds": ["INBOX", "STARRED", "Label_g1"],
     "subject": "Seam test thread", "snippet": "driven from a recording",
     "from": "Someone <someone@example.com>", "internalDate": "1780000000000",
     "hasAttachments": False}]}

MAIL_LABELS = ("ID\tNAME\tTYPE\n"
               "Label_g1\tgroup/summit\tuser\n"
               "Label_x\tZoom\tuser\n"
               "INBOX\tINBOX\tsystem\n")


def fixture_recording(drop=()):
    """The recording: the fixture's GitHub answers wired to the exact commands
    the producer issues, plus the twg/gmcli answers built here.

    `drop` omits commands whose joined text contains an entry, so one run can
    show what a failing source — or one card's checks — does to the rest."""
    calls = [gh(["api", "user"], {"login": ME})]

    rows = FIXTURES["search_review_requested"]
    calls.append(gh(["search", "prs", f"user-review-requested:{ME}", "--state=open",
                     "--json", PR_SEARCH, "--limit", "100"], rows))
    for row in rows:
        repo, num = row["repository"]["nameWithOwner"], row["number"]
        calls.append(gh(["pr", "view", str(num), "--repo", repo, "--json",
                         "headRefName,closingIssuesReferences"],
                        {"headRefName": row.get("headRefName"), "closingIssuesReferences": None}))

    rows = FIXTURES["search_author"]
    calls.append(gh(["search", "prs", "--author=@me", "--state=open",
                     "--json", PR_SEARCH, "--limit", "100"], rows))
    for row in rows:
        repo, num = row["repository"]["nameWithOwner"], row["number"]
        key = f"{repo}#{num}"
        calls.append(gh(["pr", "view", str(num), "--repo", repo, "--json", PR_VIEW],
                        FIXTURES["pr_view"][key]))
        calls.append(gh(["pr", "checks", str(num), "--repo", repo, "--json", "name,state"],
                        FIXTURES["pr_checks"][key]))
        calls.append(gh(["api", "graphql", "-f",
                         "query=" + review_threads_query(*repo.split("/"), num)],
                        {"data": {"repository": {"pullRequest": FIXTURES["pr_review_threads"][key]}}}))

    rows = FIXTURES["search_assignee"]
    calls.append(gh(["search", "issues", "--assignee=@me", "--state=open",
                     "--json", ISSUE_SEARCH, "--limit", "100"], rows))
    for row in rows:
        repo, num = row["repository"]["nameWithOwner"], row["number"]
        key = f"{repo}#{num}"
        calls.append(gh(["issue", "view", str(num), "--repo", repo, "--json", ISSUE_VIEW],
                        FIXTURES["issue_view"][key]))
        calls.append(gh(["api", "graphql", "-f",
                         "query=" + issue_links_query(*repo.split("/"), num)],
                        {"data": {"repository": {"issue": FIXTURES["issue_linked_prs"][key]}}}))

    for key in ("BIT-9001", "RBT-9001", "FIRE-9001"):
        calls.append(answer("twg", ["jira", "workitem", "get", key, "--output", "json",
                                    "--output-summary", "none"], {"data": [jira_row(key, f"{key} ticket")]}))
    calls.append(answer("twg", ["jira", "workitem", "query", "--jql", attention.JIRA_JQL,
                                "--limit", "100", "--fields", attention.JIRA_FIELDS,
                                "--output", "json", "--output-summary", "none"], {"data": JIRA_OPEN}))
    calls.append(answer("twg", ["jira", "workitem", "query", "--jql", attention.JIRA_CLOSED_JQL,
                                "--limit", "100", "--fields", attention.JIRA_CLOSED_FIELDS,
                                "--output", "json", "--output-summary", "none"], {"data": []}))
    calls.append(answer("gmcli", [attention.MAIL_ACCOUNT, "search", attention.MAIL_QUERY,
                                  "--max", str(attention.MAIL_MAX), "--json"],
                        {"threads": [MAIL_THREAD]}))
    calls.append(answer("gmcli", [attention.MAIL_ACCOUNT, "labels", "list"], MAIL_LABELS))
    for search in (f"is:pr is:closed author:{ME}", f"is:pr is:closed reviewed-by:{ME}",
                   f"is:issue is:closed assignee:{ME}"):
        calls.append(gh(["api", "graphql", "-f",
                         f'query={{ search(type: ISSUE, query: "{search} {CLOSED_ANY}") }}'],
                        {"data": {"search": {"nodes": []}}}))
    return [c for c in calls if not any(d in " ".join(c["command"]) for d in drop)]


def cards(view):
    """key -> (section, row) for every card a surface renders, children too."""
    found = {}

    def add(section, row):
        found[row["key"]] = (section, row)
        for child in row["children"]:
            found[child["key"]] = (section, child)

    for tier in view["tiers"]:
        for row in tier["items"]:
            add(tier["key"], row)
    for row in view["drafts"]:
        add("drafts", row)
    for row in view["closed"]:
        add("closed", row)
    return found


class CollectionFromARecording(unittest.TestCase):
    def collect(self, drop=()):
        return attention.collect_view(cli=Recorded(fixture_recording(drop)))

    def test_every_source_contributes_with_its_keys_and_sections(self):
        view = self.collect()
        self.assertEqual(view["errors"], [])
        self.assertEqual(view["hidden_bots"], 3)          # bot filtering stays above the seam
        found = cards(view)
        self.assertEqual({row["chip"] for _, row in found.values()},
                         {"REVIEW", "MY PR", "ISSUE", "JIRA", "MAIL"})
        self.assertEqual(sorted(found), [
            "FIRE-90000", "acme/web-frontend#1933", "acme/web-frontend#2017",
            "acme/web-frontend#2018", "acme/edge-workers#2703",
            "acme/checkout-api#331", "acme/checkout-api#332",
            "acme/checkout-api#334", "acme/checkout-api#335",
            "RBT-700", "mail/t-seam"])
        self.assertEqual(found["acme/checkout-api#335"][0], "needs")
        self.assertEqual(found["acme/checkout-api#334"][0], "ready")
        self.assertEqual(found["RBT-700"][0], "waiting")
        self.assertEqual(found["FIRE-90000"][0], "needs")
        self.assertEqual(found["mail/t-seam"][0], "needs")
        self.assertEqual(found["acme/web-frontend#1933"][0], "drafts")
        self.assertEqual(view["closed"], [])

    def test_per_card_views_checks_and_threads_are_keyed_by_card(self):
        found = cards(self.collect())
        # #335: blocked merge, no review decision, one unresolved thread from a bot.
        card = found["acme/checkout-api#335"][1]
        self.assertEqual(card["title"], FIXTURES["pr_view"]["acme/checkout-api#335"]["title"])
        self.assertEqual([f["label"] for f in card["facts"]], ["awaiting review", "checks green", "merge blocked"])
        self.assertEqual([s["key"] for s in card["states"]], ["needs-comments"])
        self.assertTrue(card["detail"].startswith("1 unresolved thread · review-bot"))
        # #334: approved and clean, no unresolved threads — its own view, checks and threads.
        self.assertEqual([f["label"] for f in found["acme/checkout-api#334"][1]["facts"]],
                         ["approved", "checks green", "mergeable"])
        self.assertEqual(found["acme/checkout-api#334"][1]["detail"], "")
        # #1933: a draft whose one unresolved thread is waiting on the reviewer.
        draft = found["acme/web-frontend#1933"][1]
        self.assertTrue(draft["draft"])
        self.assertEqual(draft["title"], FIXTURES["pr_view"]["acme/web-frontend#1933"]["title"])
        self.assertEqual([f["label"] for f in draft["facts"]],
                         ["awaiting review", "checks green", "merge conflicts"])
        self.assertEqual(draft["detail"], "1 unresolved thread · waiting on reviewer")
        # the spec issue names the card; the issue and PR linked to it ride on
        # that card as children, not on their own.
        self.assertEqual(found["acme/checkout-api#332"][1]["chip"], "ISSUE")
        self.assertEqual([c["key"] for c in found["acme/checkout-api#331"][1]["children"]],
                         ["acme/checkout-api#332", "acme/checkout-api#335"])

    def test_jira_enrichment_and_mail_grouping_cross_the_seam(self):
        found = cards(self.collect())
        # each card's jira_issue came from a recorded `twg jira workitem get` key.
        self.assertEqual(found["acme/checkout-api#334"][1]["jira_issue"]["summary"],
                         "FIRE-9001 ticket")
        self.assertEqual(found["acme/web-frontend#2018"][1]["jira_issue"]["summary"],
                         "BIT-9001 ticket")
        # the mail card's group fact came from the recorded gmcli labels table.
        self.assertEqual(found["mail/t-seam"][1]["facts"], [{"label": "group/summit", "tone": "info"}])
        self.assertEqual(found["mail/t-seam"][1]["author"], "Someone")

    def test_one_broken_source_leaves_the_others_collecting(self):
        view = self.collect(drop=("twg jira workitem query --jql " + attention.JIRA_JQL,))
        self.assertEqual([e["where"] for e in view["errors"]], ["jira tasks"])
        self.assertIn("unrecorded command: twg jira workitem query", view["errors"][0]["output"])
        found = cards(view)
        self.assertNotIn("JIRA", {row["chip"] for _, row in found.values()})
        self.assertIn("MY PR", {row["chip"] for _, row in found.values()})
        self.assertEqual(found["acme/checkout-api#334"][0], "ready")
        self.assertEqual([f["label"] for f in found["acme/checkout-api#334"][1]["facts"]],
                         ["approved", "checks green", "mergeable"])

    def test_a_card_keeps_collecting_when_its_own_checks_are_unrecorded(self):
        view = self.collect(drop=("gh pr checks 334 --repo acme/checkout-api",))
        self.assertEqual([e["where"] for e in view["errors"]], ["checkout-api#334"])
        found = cards(view)
        self.assertEqual([f["label"] for f in found["acme/checkout-api#334"][1]["facts"]],
                         ["approved", "no checks", "mergeable"])
        self.assertEqual([f["label"] for f in found["acme/checkout-api#335"][1]["facts"]],
                         ["awaiting review", "checks green", "merge blocked"])


class TheSeam(unittest.TestCase):
    def test_unrecorded_command_raises_instead_of_answering_nothing(self):
        cli = Recorded([gh(["api", "user"], {"login": ME})])
        self.assertEqual(cli.json("gh", ["api", "user"]), {"login": ME})
        for call in (lambda: cli.json("gh", ["search", "prs", "--author=@me"]),
                     lambda: cli.graphql("{ viewer { login } }"),
                     lambda: cli.text("gmcli", [attention.MAIL_ACCOUNT, "labels", "list"])):
            with self.subTest(call=call):
                self.assertRaises(LookupError, call)

    def test_a_recorded_failure_raises_the_cli_error_contract(self):
        args = ["pr", "checks", "1", "--repo", "o/r", "--json", "name,state"]
        cli = Recorded([answer("gh", args, "no checks reported on the 'fix/x' branch", code=1)])
        with self.assertRaises(attention.CliError) as caught:
            cli.json("gh", args)
        self.assertEqual(caught.exception.command, "gh pr checks 1 --repo o/r --json name,state")
        self.assertEqual(caught.exception.output, "no checks reported on the 'fix/x' branch")


if __name__ == "__main__":
    unittest.main()
