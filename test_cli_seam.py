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
twg (Jira) answers are built here, since no live recording is
committed.
"""

import json
import re
import subprocess
import threading
import unittest
from unittest import mock
from pathlib import Path

import attention

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "gh_output.json").read_text())
ME = FIXTURES["me"]

# The wire shapes the fixture was recorded from; a command the producer issues
# with different arguments is a different recorded command.
PR_SEARCH = "number,title,repository,author,createdAt,updatedAt,isDraft,url,labels"
ISSUE_SEARCH = "number,title,repository,createdAt,updatedAt,url,commentsCount,labels"

# A GraphQL `search(type: ISSUE)` query keys on the search it asks for, with its
# `closed:>=` date normalized — the producer reads that date from its own clock.
# The Jira batch keys on `key in (…)` alone: which cards asked for which key is
# what the view assertions below pin, not the recording.
SEARCH = re.compile(r'search\(type: ISSUE.*?query: "([^"]*)"')
IN_KEYS = re.compile(r"key in \([^)]*\)")
CLOSED_DATE = re.compile(r"closed:>=\d{4}-\d{2}-\d{2}")
CLOSED_ANY = "closed:>=<date>"


def key(command, args):
    """The recording's key for one invocation: exact args, except the two
    commands whose arguments are computed — a search keys on its normalized
    search alone, and the Jira batch on its key list alone."""
    keys = []
    for arg in args:
        arg = IN_KEYS.sub("key in (<keys>)", arg)
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


# One batched read is one aliased query. The aliasing is written out here, so a
# ref the producer stops asking about — or starts asking differently — is a
# different recorded command and raises; the field lists come from the producer,
# so this file pins the shape of the read, not its newest field.
def batch_query(refs, selection, field="pullRequest"):
    parts = []
    for i, (repo, number) in enumerate(refs):
        owner, name = repo.split("/")
        parts.append(f'n{i}: repository(owner:{json.dumps(owner)}, name:{json.dumps(name)}) '
                     f'{{ {field}(number:{number}) {{ {selection} }} }}')
    return "query { " + " ".join(parts) + " }"


def refs_of(rows):
    return [(row["repository"]["nameWithOwner"], row["number"]) for row in rows]


def card_key(row):
    return f'{row["repository"]["nameWithOwner"]}#{row["number"]}'


CHECKS_IN_FLIGHT = {"COMPLETED", "IN_PROGRESS", "PENDING", "QUEUED", "REQUESTED", "WAITING"}


def check_node(check):
    """gh's `pr checks --json name,state` row -> the rollup context it came from:
    a run still going reports a status, a finished one its conclusion."""
    state = check.get("state") or ""
    if state in CHECKS_IN_FLIGHT:
        return {"__typename": "CheckRun", "name": check.get("name", ""), "status": state,
                "conclusion": None}
    return {"__typename": "CheckRun", "name": check.get("name", ""), "status": "COMPLETED",
            "conclusion": state}


def pr_node(view, checks, threads):
    """The fixture's own pr view + pr checks + reviewThreads, in the one shape
    the batched query answers with."""
    return {
        "number": view["number"], "title": view["title"], "isDraft": view.get("isDraft"),
        "reviewDecision": view.get("reviewDecision"), "mergeStateStatus": view.get("mergeStateStatus"),
        "state": view.get("state"), "url": view["url"], "updatedAt": view.get("updatedAt"),
        "createdAt": view.get("createdAt"), "headRefName": view.get("headRefName"),
        "body": view.get("body"), "author": view.get("author") or {},
        "labels": {"nodes": [{"name": l.get("name"), "color": l.get("color")}
                             for l in view.get("labels") or []]},
        "closingIssuesReferences": {"nodes": view.get("closingIssuesReferences") or []},
        "statusCheckRollup": {"contexts": {"nodes": [check_node(c) for c in checks or []]}},
        "reviewThreads": (threads or {}).get("reviewThreads") or {"nodes": []},
    }


def issue_node(view, links):
    """The fixture's own issue view + linked PRs, in the one shape the batched
    query answers with."""
    return {
        "number": view["number"], "title": view["title"], "url": view["url"], "body": view.get("body"),
        "updatedAt": view.get("updatedAt"), "createdAt": view.get("createdAt"),
        "labels": {"nodes": [{"name": l.get("name"), "color": l.get("color")}
                             for l in view.get("labels") or []]},
        "comments": {"nodes": [{"author": c.get("author") or {}, "body": c.get("body")}
                               for c in view.get("comments") or []]},
        "parent": (links or {}).get("parent"),
        "subIssues": (links or {}).get("subIssues") or {"nodes": []},
        "closedByPullRequestsReferences": (links or {}).get("closedByPullRequestsReferences", None)
                                          or {"nodes": []},
    }


def jira_row(key, summary, status="In Progress", category="In Progress", comments=0):
    """One twg workitem row, with the fields the producer reads — `comment.total`
    among them, which is what the open read takes from a ticket's comments."""
    return {"key": key, "summary": summary,
            "status": {"name": status, "statusCategory": {"name": category}},
            "issuetype": {"name": "Task"}, "description": None,
            "comment": {"total": comments},
            "updated": "2026-09-28T10:00:00.000+0200", "created": "2026-09-18T10:00:00.000+0200",
            "url": attention.JIRA_BASE + key}


JIRA_OPEN = [jira_row("RBT-700", "Waiting on a reviewer"),
             jira_row("FIRE-90000", "Not started yet", status="To Do", category="To Do")]




def fixture_recording(drop=()):
    """The recording: the fixture's GitHub answers wired to the exact commands
    the producer issues, plus the twg answers built here.

    `drop` omits commands whose joined text contains an entry, so one run can
    show what a failing source — or one card's checks — does to the rest."""
    calls = [gh(["api", "user"], {"login": ME})]

    rows = FIXTURES["search_review_requested"]
    calls.append(gh(["search", "prs", f"user-review-requested:{ME}", "--state=open",
                     "--json", PR_SEARCH, "--limit", "100"], rows))
    calls.append(gh(["api", "graphql", "-f",
                     "query=" + batch_query(refs_of(rows), attention._REVIEW_SELECTION)],
                    {"data": {f"n{i}": {"pullRequest": {"headRefName": row.get("headRefName"),
                                                        "closingIssuesReferences": {"nodes": []}}}
                              for i, row in enumerate(rows)}}))

    rows = FIXTURES["search_author"]
    calls.append(gh(["search", "prs", "--author=@me", "--state=open",
                     "--json", PR_SEARCH, "--limit", "100"], rows))
    calls.append(gh(["api", "graphql", "-f",
                     "query=" + batch_query(refs_of(rows), attention._PR_SELECTION)],
                    {"data": {f"n{i}": {"pullRequest": pr_node(FIXTURES["pr_view"][card_key(row)],
                                                               FIXTURES["pr_checks"].get(card_key(row)),
                                                               FIXTURES["pr_review_threads"].get(card_key(row)))}
                              for i, row in enumerate(rows)}}))

    rows = FIXTURES["search_assignee"]
    calls.append(gh(["search", "issues", "--assignee=@me", "--state=open",
                     "--json", ISSUE_SEARCH, "--limit", "100"], rows))
    calls.append(gh(["api", "graphql", "-f",
                     "query=" + batch_query(refs_of(rows), attention._ISSUE_SELECTION, field="issue")],
                    {"data": {f"n{i}": {"issue": issue_node(FIXTURES["issue_view"][card_key(row)],
                                                              FIXTURES["issue_linked_prs"].get(card_key(row)))}
                              for i, row in enumerate(rows)}}))

    # One JQL `key in (…)` answers every key the cards ask for, and leaves out
    # the one that does not exist (a guessed token in a title), which is how a
    # wrong guess unlinks. Its key list is normalized in `key()` above.
    jira_keys = ["BIT-9001", "BIT-9002", "FIRE-9001", "RBT-9001"]
    calls.append(answer("twg", ["jira", "workitem", "query",
                                "--jql", "key in (%s)" % ", ".join(jira_keys),
                                "--limit", "100", "--fields", attention.JIRA_OPEN_FIELDS,
                                "--output", "json", "--output-summary", "none"],
                        {"data": [jira_row(k, f"{k} ticket") for k in jira_keys if k != "BIT-9002"]}))
    calls.append(answer("twg", ["jira", "workitem", "query", "--jql", attention.JIRA_JQL,
                                "--limit", "100", "--fields", attention.JIRA_OPEN_FIELDS,
                                "--output", "json", "--output-summary", "none"], {"data": JIRA_OPEN}))
    calls.append(answer("twg", ["jira", "workitem", "query", "--jql", attention.JIRA_CLOSED_JQL,
                                "--limit", "100", "--fields", attention.JIRA_CLOSED_FIELDS,
                                "--output", "json", "--output-summary", "none"], {"data": []}))
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
                         {"REVIEW", "MY PR", "ISSUE", "JIRA"})
        # sorted(): the Jira keys are upper-case, the repos are not, so they lead
        self.assertEqual(sorted(found), [
            "FIRE-90000", "RBT-700", "acme/checkout-api#331", "acme/checkout-api#332",
            "acme/checkout-api#334", "acme/checkout-api#335", "acme/edge-workers#2703",
            "acme/web-frontend#1933", "acme/web-frontend#2017", "acme/web-frontend#2018"])
        self.assertEqual(found["acme/checkout-api#335"][0], "needs")
        self.assertEqual(found["acme/checkout-api#334"][0], "ready")
        self.assertEqual(found["RBT-700"][0], "waiting")
        self.assertEqual(found["FIRE-90000"][0], "needs")
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

    def test_jira_enrichment_crosses_the_seam(self):
        found = cards(self.collect())
        # each card's jira_issue came from the one recorded `twg jira workitem query`.
        self.assertEqual(found["acme/checkout-api#334"][1]["jira_issue"]["summary"],
                         "FIRE-9001 ticket")
        self.assertEqual(found["acme/web-frontend#2018"][1]["jira_issue"]["summary"],
                         "BIT-9001 ticket")

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

    def test_a_batched_read_failing_costs_only_its_own_source(self):
        """One call now serves a whole source, so one broken call costs that
        source — and every other source still lands."""
        view = self.collect(drop=(attention._PR_SELECTION[:40],))
        self.assertEqual([e["where"] for e in view["errors"]], ["my PRs"])
        found = cards(view)
        self.assertNotIn("MY PR", {row["chip"] for _, row in found.values()})
        self.assertIn("ISSUE", {row["chip"] for _, row in found.values()})
        self.assertIn("acme/checkout-api#332", found)

    def test_a_card_left_out_of_an_answer_costs_only_that_card(self):
        """A ref the answer does not carry is that card's own read coming back
        empty, the way it was when every card was asked for separately: the rest
        of the source still lands, and nothing is reported as broken."""
        calls = []
        for call in fixture_recording():
            if "pullRequest(number:1933)" in " ".join(call["command"]):
                payload = json.loads(call["stdout"])
                payload["data"].pop("n2")                    # the third PR answers nothing
                call = dict(call, stdout=json.dumps(payload))
            calls.append(call)
        view = attention.collect_view(cli=Recorded(calls))
        found = cards(view)
        self.assertEqual(view["errors"], [])
        self.assertNotIn("acme/web-frontend#1933", found)
        self.assertIn("acme/checkout-api#334", found)


class TheSeam(unittest.TestCase):
    def test_unrecorded_command_raises_instead_of_answering_nothing(self):
        cli = Recorded([gh(["api", "user"], {"login": ME})])
        self.assertEqual(cli.json("gh", ["api", "user"]), {"login": ME})
        for call in (lambda: cli.json("gh", ["search", "prs", "--author=@me"]),
                     lambda: cli.graphql("{ viewer { login } }"),
                     lambda: cli.text("twg", ["jira", "workitem", "get", "RBT-1", "--output", "json"])):
            with self.subTest(call=call):
                self.assertRaises(LookupError, call)

    def test_a_tool_that_never_answers_is_an_error_not_a_held_slot(self):
        """A hung call keeps its slot for good: sixteen of those and every later
        read waits on the semaphore, which reads as a board that loads nothing and
        says nothing. A read this slow is a failed read, reported like any other."""
        seen = {}

        def never(*args, **kwargs):
            seen.update(kwargs)
            raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout") or 0)

        with mock.patch.object(attention.subprocess, "run", never):
            with self.assertRaises(attention.CliError) as caught:
                attention.Cli().text("gh", ["api", "user"])
        self.assertEqual(seen.get("timeout"), attention.CLI_TIMEOUT)   # the ceiling is asked for
        self.assertIn("timed out", caught.exception.output)

    def test_a_recorded_failure_raises_the_cli_error_contract(self):
        args = ["pr", "checks", "1", "--repo", "o/r", "--json", "name,state"]
        cli = Recorded([answer("gh", args, "no checks reported on the 'fix/x' branch", code=1)])
        with self.assertRaises(attention.CliError) as caught:
            cli.json("gh", args)
        self.assertEqual(caught.exception.command, "gh pr checks 1 --repo o/r --json name,state")
        self.assertEqual(caught.exception.output, "no checks reported on the 'fix/x' branch")


def comment_row(text, *, at="2026-10-02T11:00:00.000+0200", who="Ada", account="acc-ada", public=True):
    """One row of `twg jira workitem comment query --output json`."""
    return {"author": {"displayName": who, "accountId": account}, "created": at, "jsdPublic": public,
            "body": {"type": "doc", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": text}]}]}}


class Counting(Recorded):
    """The recording, counting what it answers: a cache is only a cache if the
    second look does not cross the seam again."""

    def __init__(self, calls):
        super().__init__(calls)
        self.count = 0

    def text(self, command, args):
        self.count += 1
        return super().text(command, args)


class TheCommentRead(unittest.TestCase):
    """A card's comments are read at the time of the look, one call per card,
    and reused until the card moves. Everything here is a string a page sent or
    a CLI answered, so the edges are the interesting ones."""

    def setUp(self):
        attention.COMMENTS.clear()
        attention._JIRA_ME.clear()

    def jira(self, key, comments):
        return answer("twg", ["jira", "workitem", "comment", "query", "--issue-id", key,
                               "--first", str(attention.COMMENT_MAX), "--order-by=-created",
                               "--output", "json", "--output-summary", "none"],
                      {"data": comments})

    def me(self, account="acc-me"):
        return answer("twg", ["whoami", "--output", "json", "--output-summary", "none"],
                      {"data": {"accountId": account}})

    def test_adf_is_words_not_markup(self):
        """Jira sends a comment body as ADF; the line wants the words."""
        self.assertEqual(attention.adf_text({"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "one"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "two"}]}]}),
            "one two")
        self.assertEqual(attention.adf_text("plain"), "plain")
        self.assertEqual(attention.adf_text(None), "")

    def test_a_comment_names_who_wrote_it_and_whether_it_is_mine(self):
        cli = Recorded([
            self.me(),
            # the command answers newest first; the read hands them back as a thread reads
            self.jira("FIRE-1", [comment_row("rotated", at="2026-10-02T12:00:00.000+0200",
                                              who="Me", account="acc-me", public=False),
                                  comment_row("can you re-run this?")])])
        found = attention.jira_comments("FIRE-1", cli)
        self.assertEqual([(c["who"], c["mine"], c["internal"]) for c in found],
                         [("Ada", False, False), ("Me", True, True)])
        self.assertEqual([c["at"] for c in found],
                         ["2026-10-02T09:00:00Z", "2026-10-02T10:00:00Z"])   # UTC, oldest first
        self.assertEqual(found[0]["text"], "can you re-run this?")

    def test_only_a_jira_key_is_read_and_the_answer_is_reused_until_the_card_moves(self):
        """The cache key is the card's own stamp: an unmoved card is not read
        twice, a card that has moved is read again."""
        class Shifting(attention.Cli):
            """A ticket whose thread grew between two looks."""

            def __init__(self):
                self.reads = 0

            def text(self, command, args):
                if args[:1] == ["whoami"]:
                    return json.dumps({"data": {"accountId": "acc-me"}})
                self.reads += 1
                return json.dumps({"data": [comment_row("hello" if self.reads == 1 else "moved on")]})

        cli = Shifting()
        with mock.patch.object(attention, "card_stamps", lambda: {"FIRE-1": "stamp-a"}):
            # a key off the card is not a ticket: a page can send anything, and
            # this one would be an argument to a CLI
            self.assertEqual(attention.live_comments(["--jql", "o/r#1", "FIRE-1"], cli).keys(), {"FIRE-1"})
            self.assertEqual(cli.reads, 1)
            self.assertEqual(attention.live_comments(["FIRE-1"], cli)["FIRE-1"][0]["text"], "hello")
            self.assertEqual(cli.reads, 1)              # unmoved: the answer already stood
        with mock.patch.object(attention, "card_stamps", lambda: {"FIRE-1": "stamp-b"}):
            self.assertEqual(attention.live_comments(["FIRE-1"], cli)["FIRE-1"][0]["text"], "moved on")
            self.assertEqual(cli.reads, 2)              # moved: read again

    def test_a_read_that_fails_costs_only_its_own_line(self):
        cli = Counting([self.me(), self.jira("FIRE-1", [comment_row("hello")])])
        with mock.patch.object(attention, "card_stamps", lambda: {"FIRE-1": "s", "FIRE-2": "s"}):
            out = attention.live_comments(["FIRE-2", "FIRE-1"], cli)      # FIRE-2 has no recording
            self.assertNotIn("FIRE-2", out)
            self.assertEqual(out["FIRE-1"][0]["text"], "hello")


class ThePerCardPool(unittest.TestCase):
    """A refresh is one round trip per card; this is what makes them cost one."""

    def test_the_rows_do_not_queue(self):
        # A barrier only clears with all CLI_IN_FLIGHT rows in flight at once:
        # read them one at a time and the first wait times out.
        barrier = threading.Barrier(attention.CLI_IN_FLIGHT, timeout=5)
        seen = []

        def read(row):
            barrier.wait()
            seen.append(row)

        attention._each(read, range(attention.CLI_IN_FLIGHT))
        self.assertEqual(sorted(seen), list(range(attention.CLI_IN_FLIGHT)))


if __name__ == "__main__":
    unittest.main()
