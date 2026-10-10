#!/usr/bin/env python3
"""Fixture tests for the attention queue.

Run:  python3 -m unittest -v test_view_model

Feeds recorded real `gh` output (fixtures/gh_output.json) through the adapters
and the pure view-model seam. External behavior only: states, tiers, ordering,
bot filtering + hidden count, and error passthrough.
"""

import io
import json
import os
import tempfile
import threading
import unittest
from unittest import mock
import contextlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import attention

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "gh_output.json").read_text())
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
ME = FIXTURES["me"]


def build():
    pr_items = attention.items_from_own_prs(
        FIXTURES["search_author"], FIXTURES["pr_view"], FIXTURES["pr_checks"], FIXTURES["pr_review_threads"], ME)
    issue_items = attention.items_from_issues(
        FIXTURES["search_assignee"], FIXTURES["issue_view"], FIXTURES["issue_linked_prs"], ME)
    review_items, hidden = attention.items_from_review_search(FIXTURES["search_review_requested"])
    return attention.build_view(review_items + pr_items + issue_items, hidden_bots=hidden, now=NOW), hidden


def tier(view, key):
    return next(t for t in view["tiers"] if t["key"] == key)


def refs(view, key):
    return [i["ref"] for i in tier(view, key)["items"]]


def find(view, ref):
    for i in view["drafts"]:
        if i["ref"] == ref:
            return i
    for t in view["tiers"]:
        for i in t["items"]:
            if i["ref"] == ref:
                return i
            for c in i.get("children", []):
                if c["ref"] == ref:
                    return c
    return None


class ReviewRequests(unittest.TestCase):
    def test_bots_hidden_and_counted(self):
        _, hidden = build()
        self.assertEqual(hidden, 3)

    def test_review_rows_land_in_needs_tier(self):
        view, _ = build()
        review_refs = [r for r in refs(view, "needs") if r.endswith(("#2703", "#2018", "#2017"))]
        self.assertEqual(review_refs, ["web-frontend#2018", "web-frontend#2017", "edge-workers#2703"])

    def test_review_row_carries_chip_age_and_link(self):
        view, _ = build()
        row = find(view, "web-frontend#2018")
        self.assertEqual(row["chip"], "REVIEW")
        self.assertEqual(row["states"][0]["key"], "review-requested")
        self.assertEqual(row["age"], "5d")
        self.assertTrue(row["url"].startswith("https://github.com/"))

    def test_review_row_carries_author_and_jira_from_branch_or_title(self):
        view, _ = build()
        row = find(view, "web-frontend#2018")
        self.assertEqual(row["author"], "lm-qinfei")
        self.assertEqual(row["jira"], "https://launchmetrics.atlassian.net/browse/BIT-9001")
        self.assertEqual(row["container"], "acme/web-frontend")
        # no key in the branch: the bare key in the title names the ticket
        self.assertIn("BIT-9002", find(view, "web-frontend#2017")["title"])
        self.assertEqual(find(view, "web-frontend#2017")["jira"],
                         "https://launchmetrics.atlassian.net/browse/BIT-9002")

    def test_labels_are_normalized(self):
        rows = [{"number": 1, "title": "t", "url": "u", "isDraft": False,
                 "createdAt": "2026-09-29T10:00:00Z", "updatedAt": "2026-09-29T10:00:00Z",
                 "repository": {"nameWithOwner": "o/r"}, "author": {"login": "someone"},
                 "labels": [{"id": "x", "name": "bug", "description": "d", "color": "d73a4a"}]}]
        items, _ = attention.items_from_review_search(rows)
        self.assertEqual(items[0]["labels"], [{"name": "bug", "color": "d73a4a"}])


class OwnPRs(unittest.TestCase):
    def test_335_needs_comments_from_bot_thread(self):
        view, _ = build()
        row = find(view, "checkout-api#335")
        self.assertEqual([s["key"] for s in row["states"]], ["needs-comments"])
        self.assertTrue(row["detail"].startswith(
            "1 unresolved thread · review-bot: **[NIT]**"), row["detail"])
        self.assertIn("checkout-api#331", refs(view, "needs"))

    def test_334_ready(self):
        view, _ = build()
        row = find(view, "checkout-api#334")
        self.assertEqual([s["key"] for s in row["states"]], ["ready"])
        self.assertEqual([f["label"] for f in row["facts"]],
                         ["approved", "checks green", "mergeable"])
        self.assertIn("checkout-api#334", refs(view, "ready"))

    def test_own_pr_author_and_jira_from_branch(self):
        view, _ = build()
        row = find(view, "checkout-api#334")
        self.assertEqual(row["author"], "albertvila")
        self.assertEqual(row["jira"], "https://launchmetrics.atlassian.net/browse/FIRE-9001")
        self.assertEqual(row["container"], "acme/checkout-api")
        self.assertEqual(row["labels"], [])
        row = find(view, "web-frontend#1933")
        self.assertEqual(row["jira"], "https://launchmetrics.atlassian.net/browse/RBT-9001")

    def test_a_pr_title_key_links_as_a_guess(self):
        """A PR whose branch has no key and whose body has no browse URL links
        from the bare key in its title — as a guess, so a guess Jira cannot
        resolve unlinks without a word, while a deliberate link still warns."""
        view = {"number": 7, "title": "fix: RBT-9005 pick the ES proxy by stage instead of prod",
                "url": "https://github.com/o/r/pull/7", "author": {"login": "me"},
                "headRefName": "fix/rbt-1010-es-proxy-stage", "labels": [], "body": "no link here",
                "isDraft": False, "reviewDecision": "", "mergeStateStatus": "BLOCKED", "state": "OPEN",
                "closingIssuesReferences": [], "createdAt": "2026-09-29T08:00:00Z",
                "updatedAt": "2026-09-29T09:00:00Z"}
        item = attention._pr_item(view, ["waiting"], [], {})
        self.assertEqual(item["jira"], "https://launchmetrics.atlassian.net/browse/RBT-9005")
        self.assertTrue(item["jira_guess"])
        # a key in the branch, or a browse URL in the body, is deliberate instead
        for deliberate in (dict(view, headRefName="fix/RBT-9005-es-proxy"),
                           dict(view, body="Ticket: https://launchmetrics.atlassian.net/browse/RBT-9005")):
            self.assertNotIn("jira_guess", attention._pr_item(deliberate, ["waiting"], [], {}))
        # the review queue and the closed log follow the same rule
        self.assertTrue(attention._review_item(
            {"number": 8, "title": "RBT-9005 review me", "url": "u", "headRefName": "fix/nokey",
             "repository": {"nameWithOwner": "o/r"}, "author": {"login": "them"}, "labels": []})["jira_guess"])
        self.assertTrue(attention._closed_pr_item(
            {"number": 9, "title": "fix: RBT-9005 merged", "url": "u", "state": "MERGED",
             "closedAt": "2026-09-29T09:00:00Z", "createdAt": "2026-09-01T09:00:00Z",
             "headRefName": "fix/nokey", "repository": {"nameWithOwner": "o/r"},
             "author": {"login": "me"}, "labels": {"nodes": []},
             "closingIssuesReferences": {"nodes": []}}, "MY PR")["jira_guess"])

        def missing(key):
            raise RuntimeError(f"no such ticket {key}")

        errors = []
        attention._with_jira([item], errors, fetch=missing)
        self.assertEqual((item["jira"], errors), ("", []))       # a guess unlinks, silently
        errors = []
        attention._with_jira([attention._item("MY PR", ref="o/r#7",
                                              jira="https://launchmetrics.atlassian.net/browse/RBT-9")],
                             errors, fetch=missing)
        self.assertEqual([e["where"] for e in errors], ["jira RBT-9"])   # a link that breaks still says so

    def test_draft_goes_to_its_own_section_not_the_tiers(self):
        view, _ = build()
        row = find(view, "web-frontend#1933")
        self.assertTrue(row["draft"])
        self.assertEqual([s["key"] for s in row["states"]], ["waiting-reply", "conflicts"])
        self.assertEqual(row["detail"], "1 unresolved thread · waiting on reviewer")
        self.assertEqual([r["ref"] for r in view["drafts"]], ["web-frontend#1933"])
        self.assertNotIn("web-frontend#1933",
                         [r["ref"] for t in view["tiers"] for r in t["items"]])

    def test_unresolved_thread_only_needs_me_when_i_did_not_reply_last(self):
        view = {"state": "OPEN"}
        mine = {"reviewThreads": {"nodes": [{"isResolved": False,
                                              "comments": {"nodes": [{"author": {"login": "me"},
                                                                        "body": "done"}]}}]}}
        theirs = {"reviewThreads": {"nodes": [{"isResolved": False,
                                                "comments": {"nodes": [{"author": {"login": "you"},
                                                                          "body": "nit"}]}}]}}
        self.assertEqual(attention.pr_states(view, [], mine, me="me"), ["waiting-reply"])
        self.assertEqual(attention.pr_states(view, [], theirs, me="me"), ["needs-comments"])

    def test_pr_facts(self):
        view = {"reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN"}
        self.assertEqual(attention._pr_facts(view, [{"name": "ci", "state": "SUCCESS"}]),
                         [{"label": "approved", "tone": "ok"},
                          {"label": "checks green", "tone": "ok"},
                          {"label": "mergeable", "tone": "ok"}])
        view = {"reviewDecision": "REVIEW_REQUIRED", "mergeStateStatus": "BLOCKED"}
        self.assertEqual(attention._pr_facts(view, []),
                         [{"label": "awaiting review", "tone": "warn"},
                          {"label": "no checks", "tone": "warn"},
                          {"label": "merge blocked", "tone": "warn"}])

    def test_unresolved_thread_alone_is_one_state(self):
        view_335 = FIXTURES["pr_view"]["acme/checkout-api#335"]
        # all checks green, no approval, blocked: only the unresolved thread
        self.assertEqual(attention.pr_states(view_335, FIXTURES["pr_checks"]["acme/checkout-api#335"],
                                       FIXTURES["pr_review_threads"]["acme/checkout-api#335"], ME),
                         ["needs-comments"])

    def test_conflicts_state_and_tier(self):
        view = {"state": "OPEN", "isDraft": True, "mergeStateStatus": "DIRTY",
                "reviewDecision": "REVIEW_REQUIRED"}
        self.assertEqual(attention.pr_states(view, [], {"reviewThreads": {"nodes": []}}), ["conflicts"])
        threads = {"reviewThreads": {"nodes": [{"isResolved": False,
                                                   "comments": {"nodes": [{"author": {"login": "someone"}}]}}]}}
        self.assertEqual(attention.pr_states(view, [], threads), ["needs-comments", "conflicts"])
        built = attention.build_view([attention._item("", ref="r#1", states=["conflicts"])], now=NOW)
        self.assertEqual(built["tiers"][0]["items"][0]["ref"], "r#1")

    def test_ci_failing(self):
        failing = [{"name": "build", "state": "FAILURE"}]
        view = {"state": "OPEN", "reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN", "isDraft": False}
        self.assertEqual(attention.pr_states(view, failing, {"reviewThreads": {"nodes": []}}), ["ci-failing"])

    def test_running_check_is_not_failing(self):
        running = [{"name": "Unit tests (docs) / Build and test", "state": "IN_PROGRESS"}]
        view = {"state": "OPEN", "reviewDecision": "REVIEW_REQUIRED", "mergeStateStatus": "BLOCKED", "isDraft": False}
        self.assertEqual(attention.pr_states(view, running, {"reviewThreads": {"nodes": []}}), ["waiting"])
        self.assertEqual(attention._pr_facts(view, running)[1], {"label": "1 checks running", "tone": "warn"})
        self.assertEqual(attention._pr_facts(view, running + [{"name": "lint", "state": "FAILURE"}])[1],
                         {"label": "1 checks failing", "tone": "bad"})


class JiraEnrichment(unittest.TestCase):
    def test_one_fetch_per_key_and_view_model_passthrough(self):
        items = [attention._item("", ref="r#1", states=["ready"], jira="https://x/browse/FIRE-1"),
                 attention._item("", ref="r#2", states=["ready"], jira="https://x/browse/FIRE-1"),
                 attention._item("", ref="r#3", states=["ready"])]
        calls = []

        def fetch(keys):
            calls.append(keys)
            return {k: {"summary": "s", "status": "Open", "status_category": "To Do", "type": "Bug"}
                    for k in keys if k != "FIRE-9"}

        attention._with_jira(items, [], fetch=fetch)
        self.assertEqual(calls, [["FIRE-1"]])          # every key in one query, the shared one only once
        self.assertEqual(items[0]["jira_issue"]["summary"], "s")
        view = attention.build_view(items, now=NOW)
        # r#1 and r#2 share a Jira key, so they cluster; r#3 stays separate.
        header = find(view, "r#1")
        self.assertEqual([c["ref"] for c in header["children"]], ["r#2"])
        self.assertEqual(header["jira_issue"]["summary"], "s")
        self.assertIsNone(find(view, "r#3")["jira_issue"])

    def test_a_failure_is_reported_not_fatal(self):
        items = [attention._item("", jira="https://x/browse/FIRE-1")]
        errors = []

        def fetch(keys):
            raise attention.CliError("twg jira workitem query", "not found")

        attention._with_jira(items, errors, fetch=fetch)
        self.assertNotIn("jira_issue", items[0])
        self.assertEqual(errors[0]["where"], "jira FIRE-1")
        self.assertEqual(errors[0]["output"], "not found")

    def test_a_key_the_answer_left_out_is_the_key_missing(self):
        """JQL quietly leaves out a key that does not exist, so nothing about the
        query failing: that key is the one with no ticket."""
        guess = attention._item("", jira="https://x/browse/UTF-8")
        guess["jira_guess"] = True                       # a key read off a title, not a link
        errors = []
        attention._with_jira([guess], errors, fetch=lambda keys: {})
        self.assertEqual(errors, [])
        self.assertEqual(guess["jira"], "")             # a guess that misses unlinks, silently

        ticket = attention._item("", ref="r#1", jira="https://x/browse/FIRE-404")
        attention._with_jira([ticket], errors, fetch=lambda keys: {})
        self.assertEqual([e["where"] for e in errors], ["jira FIRE-404"])
        self.assertIn("no ticket FIRE-404", errors[0]["output"])
        self.assertEqual(ticket["jira"], "https://x/browse/FIRE-404")   # a real card keeps its link


class JiraSource(unittest.TestCase):
    def test_jira_description_links_to_github_thread(self):
        rows = [
            {"key": "RBT-1", "summary": "s",
             "status": {"name": "Ready to Test", "statusCategory": {"name": "In Progress"}},
             "issueType": {"name": "Task"},
             "description": {"type": "doc", "content": [{"type": "paragraph", "content": [
                 {"type": "text", "text": "Spec: https://github.com/acme/checkout-api/issues/331"}]}]}},
        ]
        items = attention.items_from_jira(rows)
        self.assertEqual(items[0]["links"], ["acme/checkout-api#331"])

    def test_blocked_by_links_name_the_holder_with_its_own_status(self):
        """A card Jira reports as blocked shows the blocker: key, its status and
        summary, all of it carried by the link. A card that blocks something
        else, or merely relates to one, is not blocked by it."""
        rows = [
            {"key": "FIRE-1", "summary": "held up",
             "status": {"name": "Waiting for Customer", "statusCategory": {"name": "In Progress"}},
             "issueType": {"name": "Bug"},
             "issuelinks": [
                 {"type": {"name": "Blocking Issue", "inward": "is blocked by", "outward": "blocks"},
                  "inwardIssue": {"key": "FIDI-275", "fields": {
                      "summary": "soda checks fail while the enrichment takes 2h",
                      "status": {"name": "In Progress", "statusCategory": {"name": "In Progress"}},
                      "issuetype": {"name": "Bug"}}}},
                 # this card is the blocker here, not the blocked one
                 {"type": {"name": "Blocking Issue", "inward": "is blocked by", "outward": "blocks"},
                  "outwardIssue": {"key": "FIRE-999", "fields": {}}},
                 {"type": {"name": "Relates", "inward": "relates to", "outward": "relates to"},
                  "inwardIssue": {"key": "RBT-9", "fields": {}}},
             ]},
        ]
        item = attention.items_from_jira(rows)[0]
        self.assertEqual(item["blocked_by"], [{
            "key": "FIDI-275", "url": attention.JIRA_BASE + "FIDI-275",
            "status": "In Progress", "status_category": "In Progress", "type": "Bug",
            "summary": "soda checks fail while the enrichment takes 2h"}])
        # it rides to the row the surface renders
        row = find(attention.build_view([item], now=NOW), "FIRE-1")
        self.assertEqual([b["key"] for b in row["blocked_by"]], ["FIDI-275"])
        # and a card nobody blocks says so with an empty list, never a missing key
        plain = attention.items_from_jira([{"key": "RBT-2", "summary": "free",
                                             "status": {}, "issueType": {}}])[0]
        self.assertEqual(plain["blocked_by"], [])

    def test_a_jira_link_is_named_with_the_other_tickets_own_status(self):
        """The ticket you raised with another team and are waiting on is named on
        the card: key, its type, status and summary, all of it carried by the
        link. A blocking link is not here — that one is blocked_by — and the open
        tickets come first, because that one is the wait."""
        relates = {"name": "Relates", "inward": "relates to", "outward": "relates to"}
        blocks = {"name": "Blocking Issue", "inward": "is blocked by", "outward": "blocks"}
        rows = [{"key": "FIRE-77999", "summary": "high findings disclosed",
                 "status": {"name": "Support Investigating", "statusCategory": {"name": "In Progress"}},
                 "issueType": {"name": "Data"},
                 "issuelinks": [
                     {"type": relates, "inwardIssue": {"key": "RBT-433", "fields": {
                         "summary": "the discarded work ticket", "issuetype": {"name": "Bug"},
                         "status": {"name": "Discard", "statusCategory": {"name": "Done"}}}}},
                     {"type": relates, "outwardIssue": {"key": "FISRE-27667", "fields": {
                         "summary": "rotate the leaked secrets", "issuetype": {"name": "Service Request"},
                         "status": {"name": "In Progress", "statusCategory": {"name": "In Progress"}}}}},
                     {"type": blocks, "inwardIssue": {"key": "FIDI-1", "fields": {}}},
                 ]}]
        item = attention.items_from_jira(rows)[0]
        self.assertEqual(item["linked"], [
            {"key": "FISRE-27667", "url": attention.JIRA_BASE + "FISRE-27667",
             "status": "In Progress", "status_category": "In Progress", "type": "Service Request",
             "summary": "rotate the leaked secrets"},
            {"key": "RBT-433", "url": attention.JIRA_BASE + "RBT-433",
             "status": "Discard", "status_category": "Done", "type": "Bug",
             "summary": "the discarded work ticket"}])
        self.assertEqual([b["key"] for b in item["blocked_by"]], ["FIDI-1"])
        # it rides to the row the surface renders
        row = find(attention.build_view([item], now=NOW), "FIRE-77999")
        self.assertEqual([l["key"] for l in row["linked"]], ["FISRE-27667", "RBT-433"])
        # and a card nobody links to says so with an empty list, never a missing key
        plain = attention.items_from_jira([{"key": "RBT-2", "summary": "free",
                                             "status": {}, "issueType": {}}])[0]
        self.assertEqual(plain["linked"], [])

    def test_a_link_the_card_already_carries_is_not_named_twice(self):
        """The ticket a Jira link joins to this one is already on the card — as a
        child, or inline when it names the header — so the linked line does not
        name it again."""
        links = {"name": "Problem/Incident", "inward": "is caused by", "outward": "causes"}
        rows = [
            {"key": "RBT-9004", "summary": "the work",
             "status": {"name": "Ready to Test", "statusCategory": {"name": "In Progress"}},
             "issueType": {"name": "Bug"},
             "issuelinks": [{"type": links, "outwardIssue": {"key": "FIRE-97142", "fields": {
                 "summary": "the alert", "issuetype": {"name": "Bug"},
                 "status": {"name": "To Do", "statusCategory": {"name": "To Do"}}}}}]},
            {"key": "FIRE-97142", "summary": "the alert",
             "status": {"name": "To Do", "statusCategory": {"name": "To Do"}},
             "issueType": {"name": "Bug"},
             "issuelinks": [{"type": links, "inwardIssue": {"key": "RBT-9004", "fields": {}}}]},
        ]
        card = find(attention.build_view(attention.items_from_jira(rows), now=NOW), "RBT-9004")
        self.assertEqual([c["ref"] for c in card["children"]], ["FIRE-97142"])
        self.assertEqual(card["linked"], [])

    def test_a_blocking_link_is_a_dependency_not_a_cluster_link(self):
        """A blocker stays its own card — it rides in blocked_by — so a blocking
        link never clusters the two tickets, in either direction."""
        rows = [{"key": "FIRE-1", "summary": "held up",
                 "status": {"name": "In Progress", "statusCategory": {"name": "In Progress"}},
                 "issueType": {},
                 "issuelinks": [
                     {"type": {"name": "Blocking Issue", "inward": "is blocked by",
                               "outward": "blocks"},
                      "inwardIssue": {"key": "RBT-9", "fields": {}}},
                     {"type": {"name": "Blocking Issue", "inward": "is blocked by",
                               "outward": "blocks"},
                      "outwardIssue": {"key": "RBT-8", "fields": {}}},
                 ]}]
        item = attention.items_from_jira(rows)[0]
        self.assertEqual(item["links"], [])
        self.assertEqual([b["key"] for b in item["blocked_by"]], ["RBT-9"])

    def test_jira_issue_link_makes_one_card_and_the_fireline_alert_yields(self):
        """Two tickets Jira links together — Fireline's alert to the ticket that
        caused it — become one card: the work ticket names it and its state lands
        it, so the alert's untouched To Do does not drag the card to Needs."""
        links = {"name": "Problem/Incident", "inward": "is caused by", "outward": "causes"}
        rows = [
            {"key": "RBT-9004", "summary": "the work",
             "status": {"name": "Ready to Test", "statusCategory": {"name": "In Progress"}},
             "issueType": {"name": "Bug"},
             "issuelinks": [{"type": links, "outwardIssue": {"key": "FIRE-97142", "fields": {}}}]},
            {"key": "FIRE-97142", "summary": "the alert",
             "status": {"name": "To Do", "statusCategory": {"name": "To Do"}},
             "issueType": {"name": "Bug"},
             "issuelinks": [{"type": links, "inwardIssue": {"key": "RBT-9004", "fields": {}}}]},
        ]
        items = attention.items_from_jira(rows)
        self.assertEqual(items[0]["links"], ["FIRE-97142"])
        self.assertEqual(items[1]["links"], ["RBT-9004"])
        view = attention.build_view(items, now=NOW)
        self.assertEqual(refs(view, "waiting"), ["RBT-9004"])
        self.assertEqual(refs(view, "needs"), [])
        card = find(view, "RBT-9004")
        self.assertEqual(card["tier"], "waiting")
        self.assertEqual([c["ref"] for c in card["children"]], ["FIRE-97142"])
        self.assertEqual(card["children"][0]["states"][0]["key"], "not-started")

    def test_a_fireline_alert_alone_lands_by_its_own_status(self):
        rows = [{"key": "FIRE-1", "summary": "alert",
                 "status": {"name": "To Do", "statusCategory": {"name": "To Do"}},
                 "issueType": {}}]
        view = attention.build_view(attention.items_from_jira(rows), now=NOW)
        self.assertEqual(refs(view, "needs"), ["FIRE-1"])
        self.assertEqual(refs(view, "waiting"), [])

    def test_maps_category_type_url_and_jira_offset(self):
        rows = [
            {"key": "RBT-1", "summary": "s", "url": "https://x/browse/RBT-1",
             "status": {"name": "Ready to Test", "statusCategory": {"name": "In Progress"}},
             "issueType": {"name": "Task"},
             "updated": "2026-09-28T15:51:28.377+0200", "created": "2026-09-01T10:00:00.000+0200"},
            {"key": "RBT-2", "summary": "t",
             "status": {"name": "To Do", "statusCategory": {"name": "To Do"}},
             "issueType": {"name": "Bug"}},
            {"key": "RBT-3", "summary": "done",
             "status": {"name": "Done", "statusCategory": {"name": "Done"}}, "issueType": {}},
            {"key": "RBT-4", "summary": "to deploy",
             "status": {"name": "TO_DEPLOY", "statusCategory": {"name": "Done"}},
             "issueType": {"name": "Task"}},
        ]
        items = attention.items_from_jira(rows)
        self.assertEqual([i["ref"] for i in items], ["RBT-1", "RBT-2", "RBT-4"])
        self.assertEqual(items[0]["states"], ["in-progress"])
        self.assertEqual(items[0]["detail"], "Task")
        self.assertEqual(items[0]["facts"], [{"label": "Ready to Test", "tone": "warn"}])
        self.assertEqual(items[0]["times"]["updated"], "2026-09-28T15:51:28.377+02:00")
        self.assertEqual(attention.humanize_age(items[0]["times"]["updated"], NOW), "22h")
        self.assertEqual(items[1]["states"], ["not-started"])
        self.assertEqual(items[1]["facts"], [{"label": "To Do", "tone": "waiting"}])
        self.assertEqual(items[1]["url"], "https://launchmetrics.atlassian.net/browse/RBT-2")
        self.assertEqual(items[2]["states"], ["to-deploy"])
        self.assertEqual(items[2]["facts"], [{"label": "TO_DEPLOY", "tone": "warn"}])
        view = attention.build_view(items, now=NOW)
        self.assertIn("RBT-1", [r["ref"] for r in view["tiers"][2]["items"]])
        self.assertIn("RBT-2", [r["ref"] for r in view["tiers"][0]["items"]])
        self.assertIn("RBT-4", [r["ref"] for r in view["tiers"][1]["items"]])

    def test_support_investigating_is_waiting_with_the_status_as_fact(self):
        rows = [
            {"key": "FIRE-1", "summary": "with support",
             "status": {"name": "Support Investigating", "statusCategory": {"name": "In Progress"}},
             "issueType": {"name": "Task"}},
        ]
        item = attention.items_from_jira(rows)[0]
        self.assertEqual(item["states"], ["with-support"])
        self.assertEqual(item["facts"], [{"label": "Support Investigating", "tone": "warn"}])
        view = attention.build_view([item], now=NOW)
        row = find(view, "FIRE-1")
        self.assertEqual(row["states"][0]["tier"], "waiting")
        self.assertEqual(row["states"][0]["label"], "with support")
        # no new section: it rides the waiting tier like any other waiting card
        snapshot = attention.payload(view, now=NOW)
        self.assertEqual(snapshot["tiers"][2]["items"][0]["section"], "waiting")

    def test_only_the_support_status_maps_to_with_support(self):
        """Waiting for Customer keeps the category mapping, a ticket that has not
        started stays in Needs, and to-deploy stays in Ready."""
        rows = [
            {"key": "FIRE-1", "summary": "a",
             "status": {"name": "Waiting for Customer", "statusCategory": {"name": "In Progress"}},
             "issueType": {}},
            {"key": "FIRE-2", "summary": "b",
             "status": {"name": "To Do", "statusCategory": {"name": "To Do"}}, "issueType": {}},
            {"key": "FIRE-3", "summary": "c",
             "status": {"name": "TO_DEPLOY", "statusCategory": {"name": "Done"}}, "issueType": {}},
            {"key": "FIRE-4", "summary": "d",
             "status": {"name": "Support Investigating", "statusCategory": {"name": "In Progress"}},
             "issueType": {}},
        ]
        items = attention.items_from_jira(rows)
        self.assertEqual([i["states"] for i in items],
                         [["in-progress"], ["not-started"], ["to-deploy"], ["with-support"]])
        view = attention.build_view(items, now=NOW)
        self.assertEqual({r["ref"]: i for i, t in enumerate(view["tiers"]) for r in t["items"]},
                         {"FIRE-1": 2, "FIRE-2": 0, "FIRE-3": 1, "FIRE-4": 2})


class Clustering(unittest.TestCase):
    def test_linked_pr_issue_spec_become_one_card(self):
        view, _ = build()
        header = find(view, "checkout-api#331")
        self.assertEqual(sorted(c["ref"] for c in header["children"]),
                         ["checkout-api#332", "checkout-api#335"])
        self.assertIn("checkout-api#331", refs(view, "needs"))
        self.assertNotIn("checkout-api#331", refs(view, "waiting"))
        self.assertNotIn("checkout-api#332", refs(view, "waiting"))
        self.assertEqual(find(view, "checkout-api#331")["labels"], [{"name": "spec", "color": "7057FF"}])

    def test_pr_title_issue_ref_links_the_issue(self):
        row = {"number": 2331, "title": "feat: #2330 Drop Instagram stories from monthly and hourly final files",
               "repository": {"nameWithOwner": "acme/payments-api"}, "author": {"login": "albertvila"},
               "url": "https://github.com/acme/payments-api/pull/2331", "labels": [],
               "isDraft": False, "createdAt": "2026-09-29T13:30:00Z", "updatedAt": "2026-09-29T14:00:00Z"}
        items, _ = attention.items_from_review_search([row])
        self.assertEqual(items[0]["links"], ["acme/payments-api#2330"])
        issue = attention._item("ISSUE", container="acme/payments-api",
                                ref="payments-api#2330", states=["waiting-reply"])
        view = attention.build_view(items + [issue], now=NOW)
        header = find(view, "payments-api#2331")
        self.assertEqual([c["ref"] for c in header["children"]], ["payments-api#2330"])

    def test_children_keep_facts_detail_and_labels(self):
        items = [
            attention._item("MY PR", container="o/r", ref="r#1", states=["waiting"],
                            detail="awaiting review", facts=[{"label": "checks green", "tone": "ok"}],
                            labels=[{"name": "bug", "color": "d73a4a"}]),
            attention._item("ISSUE", container="o/r", ref="r#2", states=["ready"], links=["o/r#1"]),
        ]
        view = attention.build_view(items, now=NOW)
        child = find(view, "r#1")
        self.assertEqual(child["facts"], [{"label": "checks green", "tone": "ok"}])
        self.assertEqual(child["detail"], "awaiting review")
        self.assertEqual(child["labels"], [{"name": "bug", "color": "d73a4a"}])

    def test_cluster_lands_in_its_most_urgent_tier(self):
        items = [
            attention._item("ISSUE", container="o/r", ref="r#2", url="u2", states=["waiting"]),
            attention._item("MY PR", container="o/r", ref="r#5", url="u5",
                            states=["needs-comments"], links=["o/r#2"]),
        ]
        view = attention.build_view(items, now=NOW)
        self.assertEqual(refs(view, "needs"), ["r#5"])
        self.assertEqual([c["ref"] for c in find(view, "r#5")["children"]], ["r#2"])
        self.assertEqual(refs(view, "waiting"), [])

    def test_issue_comments_and_body_link_explicit_urls(self):
        """A hand-written 'PR: <url>' in an issue comment links the two cards;
        that is how a hand-opened PR gets folded into its issue."""
        issue = {"number": 2048, "title": "Move data unification into PLS",
                 "url": "https://github.com/acme/web-frontend/issues/2048",
                 "body": "no links in the body",
                 "comments": [{"author": {"login": "me"},
                               "body": "opened https://github.com/acme/checkout-api/pull/349"}],
                 "labels": [], "updatedAt": "2026-09-29T10:00:00Z", "createdAt": "2026-09-29T09:00:00Z"}
        empty_rel = {"parent": None, "subIssues": {"nodes": []}, "closedByPullRequestsReferences": {"nodes": []}}
        row = {"repository": {"nameWithOwner": "acme/web-frontend"}, "number": 2048}
        item = attention.items_from_issues([row], {"acme/web-frontend#2048": issue},
                                     {"acme/web-frontend#2048": empty_rel}, "me")[0]
        self.assertEqual(item["links"], ["acme/checkout-api#349"])

        pr = attention._item("MY PR", container="acme/checkout-api", ref="checkout-api#349",
                             title="feat: move data unification", url="u", states=["waiting"],
                             times={"updated": "2026-09-29T11:00:00Z"})
        view = attention.build_view([item, pr], now=NOW)
        cards = [i for t in view["tiers"] for i in t["items"]]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["key"], "acme/checkout-api#349")
        self.assertEqual([c["key"] for c in cards[0]["children"]], ["acme/web-frontend#2048"])

    def test_draft_cluster_goes_to_drafts_with_children(self):
        items = [
            attention._item("ISSUE", container="o/r", ref="r#2", states=["waiting"]),
            attention._item("MY PR", container="o/r", ref="r#5", draft=True,
                            states=["needs-comments"], links=["o/r#2"]),
        ]
        view = attention.build_view(items, now=NOW)
        self.assertEqual([r["ref"] for r in view["drafts"]], ["r#5"])
        self.assertEqual([c["ref"] for c in view["drafts"][0]["children"]], ["r#2"])
        self.assertEqual([r["ref"] for t in view["tiers"] for r in t["items"]], [])

    def test_jira_key_and_description_join_the_cluster(self):
        items = [
            attention._item("ISSUE", container="o/r", ref="r#1", states=["waiting"], links=["o/r#2"]),
            attention._item("MY PR", container="o/r", ref="r#2", states=["waiting"],
                            jira="https://x/browse/RBT-1"),
            attention._item("JIRA", source="jira", ref="RBT-1", states=["to-deploy"]),
        ]
        view = attention.build_view(items, now=NOW)
        self.assertEqual(refs(view, "ready"), ["RBT-1"])
        self.assertEqual(sorted(c["ref"] for c in find(view, "RBT-1")["children"]),
                         ["r#1", "r#2"])
    def test_header_jira_ticket_is_not_repeated_as_child(self):
        items = [
            attention._item("MY PR", container="o/r", ref="r#5", states=["ready"],
                            jira="https://x/browse/ABC-1"),
            attention._item("JIRA", source="jira", ref="ABC-1", states=["in-progress"]),
        ]
        view = attention.build_view(items, now=NOW)
        self.assertEqual(find(view, "ABC-1")["ref"], "ABC-1")
        self.assertEqual([c["ref"] for c in find(view, "ABC-1")["children"]], ["r#5"])
class ClosedLog(unittest.TestCase):
    def test_within_window(self):
        self.assertTrue(attention._within_window("2026-09-29T08:00:00Z", NOW))
        self.assertFalse(attention._within_window("2026-09-28T08:00:00Z", NOW))
        self.assertTrue(attention._within_window("2026-09-29T10:28:56.277+02:00", NOW))
        self.assertFalse(attention._within_window(None, NOW))

    def test_closed_pr_item_marks_merged_vs_closed(self):
        node = {"number": 7, "title": "cherry-pick of #3", "url": "u", "state": "MERGED",
                "closedAt": "2026-09-29T09:00:00Z", "createdAt": "2026-09-01T09:00:00Z",
                "headRefName": "m-chore-x-ABC-1",
                "repository": {"nameWithOwner": "o/r"}, "author": {"login": "me"},
                "labels": {"nodes": [{"name": "bug", "color": "d73a4a"}]},
                "closingIssuesReferences": {"nodes": [{"number": 2, "repository": {"nameWithOwner": "o/r"}}]}}
        item = attention._closed_pr_item(node, "MY PR")
        self.assertEqual(item["states"], ["merged"])
        self.assertEqual(item["links"], ["o/r#2", "o/r#3"])
        self.assertEqual(item["jira"], "https://launchmetrics.atlassian.net/browse/ABC-1")
        self.assertEqual(item["labels"], [{"name": "bug", "color": "d73a4a"}])
        node["state"] = "CLOSED"
        item = attention._closed_pr_item(node, "REVIEWED")
        self.assertEqual(item["states"], ["closed"])
        self.assertEqual(item["chip"], "REVIEWED")

    def test_closed_pr_item_links_jira_from_its_body_too(self):
        """A merged PR whose branch carries no key has the body URL as its only
        link: the same rule the open read follows, or the card loses its ticket
        the moment the PR merges."""
        node = {"number": 2345, "title": "fix: read document deletions gold from the stage catalog",
                "url": "u", "state": "MERGED", "closedAt": "2026-09-29T09:00:00Z",
                "createdAt": "2026-09-01T09:00:00Z", "headRefName": "fix/stage-dependent-document-deletions",
                "body": "Summary.\n\nTicket: https://launchmetrics.atlassian.net/browse/RBT-9005",
                "repository": {"nameWithOwner": "o/r"}, "author": {"login": "me"},
                "labels": {"nodes": []}, "closingIssuesReferences": {"nodes": []}}
        self.assertEqual(attention._closed_pr_item(node, "MY PR")["jira"],
                         "https://launchmetrics.atlassian.net/browse/RBT-9005")

    def test_closed_issue_item_links_parent_subissues_and_prs(self):
        node = {"number": 2, "title": "t", "url": "u", "closedAt": "2026-09-29T09:00:00Z",
                "createdAt": "2026-09-01T09:00:00Z", "repository": {"nameWithOwner": "o/r"},
                "body": "Jira spec: https://launchmetrics.atlassian.net/browse/RBT-7",
                "labels": {"nodes": []},
                "parent": {"number": 1, "repository": {"nameWithOwner": "o/r"}},
                "subIssues": {"nodes": [{"number": 3, "repository": {"nameWithOwner": "o/r"}}]},
                "closedByPullRequestsReferences": {"nodes": [{"number": 5, "repository": {"nameWithOwner": "o/r"}}]}}
        item = attention._closed_issue_item(node)
        self.assertEqual(sorted(item["links"]), ["o/r#1", "o/r#3", "o/r#5"])
        self.assertEqual(item["jira"], "https://launchmetrics.atlassian.net/browse/RBT-7")

    def test_closed_jira_uses_status_change_date(self):
        rows = [{"key": "ABC-1", "summary": "s", "url": "u",
                 "status": {"name": "Done"}, "issueType": {"name": "Task"},
                 "description": {"type": "doc", "content": [{"type": "paragraph", "content": [
                     {"type": "text", "text": "Spec: https://github.com/o/r/issues/2"}]}]},
                 "statuscategorychangeddate": "2026-09-29T10:28:56.277+0200",
                 "updated": "2026-09-29T12:00:22.383+0200"}]
        item = attention.items_from_jira_closed(rows)[0]
        self.assertEqual(item["states"], ["done"])
        self.assertEqual(item["times"]["updated"], "2026-09-29T10:28:56.277+02:00")
        self.assertEqual(item["detail"], "Done · Task")
        self.assertEqual(item["links"], ["o/r#2"])

    def test_closed_items_cluster_with_their_open_links(self):
        """A merged PR and the open item it links to stay one card. No spec or
        ticket label, so the PR names it, and the merge still lands it in Ready."""
        closed = [
            attention._item("ISSUE", container="o/r", ref="r#2", states=["closed"],
                            times={"updated": "2026-09-29T09:01:00Z"}, links=["o/r#5"]),
            attention._item("MY PR", container="o/r", ref="r#5", states=["merged"],
                            times={"updated": "2026-09-29T09:00:00Z"}, links=["o/r#2"]),
        ]
        open_items = [attention._item("ISSUE", container="o/r", ref="r#9", states=["waiting"],
                                      links=["o/r#5"])]
        view = attention.build_view(open_items, now=NOW, closed=closed)
        self.assertEqual(view["closed"], [])
        cards = {t["key"]: t["items"] for t in view["tiers"]}
        self.assertEqual([r["ref"] for r in cards["ready"]], ["r#5"])
        self.assertEqual(sorted(c["ref"] for c in cards["ready"][0]["children"]),
                         ["r#2", "r#9"])

    def test_merged_pr_lifts_its_ticket_to_ready(self):
        """The ticket a merged PR names stays one card with it, and the merged
        PR is what lands it in Ready — where the follow-up is: close the ticket,
        deploy it. The ticket still heads the card, because finished work never
        names live work, so it rides struck underneath instead."""
        ticket = attention._item("JIRA", source="jira", ref="FIRE-1", states=["in-progress"],
                                 times={"updated": "2026-09-29T09:00:00Z"})
        merged = attention._item("MY PR", container="o/r", ref="r#7", states=["merged"],
                                 times={"updated": "2026-09-29T09:30:00Z"},
                                 jira=attention.JIRA_BASE + "FIRE-1")
        view = attention.build_view([ticket], now=NOW, closed=[merged])
        cards = {t["key"]: t["items"] for t in view["tiers"]}
        self.assertEqual([r["ref"] for r in cards["ready"]], ["FIRE-1"])
        self.assertEqual(cards["waiting"], [])
        self.assertEqual([c["ref"] for c in cards["ready"][0]["children"]], ["r#7"])   # merged, struck
        # the row's own tier is its header's; where it renders is the bucket, and
        # the surface draws from the bucket (`section` once with_changes has run).
        self.assertEqual(cards["ready"][0]["tier"], "waiting")
        self.assertEqual(view["closed"], [])

    def test_section_only_move_does_not_repeat_the_tier(self):
        """A card coming back out of the closed log has the same state and tier;
        the label must not read 'Waiting on others → Waiting on others'."""
        merged = attention._item("MY PR", container="o/r", ref="r#7", states=["merged"],
                                 times={"updated": "2026-09-29T09:00:00Z"})
        before = attention.payload(attention.build_view([], now=NOW, closed=[merged]), now=NOW)
        after = attention.payload(attention.build_view([dict(merged, links=["o/r#7"])], now=NOW), previous=before)
        self.assertEqual(after["changes"]["items"]["o/r#7"]["label"], "moved")

    def test_the_lane_shows_a_day_however_long_closed_work_is_collected(self):
        """Two horizons, two jobs: a finished cluster leaves the lane a day after
        it closed, while the collection that keeps a live card's closed members
        runs for as long as a ghost could appear."""
        def cluster(closed_at):
            return [attention._item("MY PR", container="o/r", ref="r#5", states=["merged"],
                                    times={"updated": closed_at})]
        fresh = attention.build_view([], now=NOW, closed=cluster("2026-09-29T10:00:00Z"))
        stale = attention.build_view([], now=NOW, closed=cluster("2026-09-26T10:00:00Z"))
        self.assertEqual([r["ref"] for r in fresh["closed"]], ["r#5"])
        self.assertEqual(stale["closed"], [])

    def test_closed_work_is_collected_past_the_lane_so_a_live_card_keeps_its_member(self):
        """A closed member of a card that is still live must keep being collected:
        the moment it stops, it falls out of the card and ghosts into the tier it
        left, as work that is still owed (web-frontend#2059, checkout-api#352)."""

        class Fake:
            """Only the reads _collect_closed makes: two graphql searches, one twg."""
            def __init__(self, prs=()):
                self.prs, self.queries = list(prs), []

            def graphql(self, query):
                self.queries.append(query)
                return {"data": {"search": {"nodes": self.prs if "is:pr" in query else []}}}

            def json(self, command, args):
                return {"data": {"issues": []}}

        days_ago = NOW - timedelta(days=3)
        node = {"number": 59, "title": "t", "url": "u", "state": "MERGED",
                "closedAt": days_ago.isoformat().replace("+00:00", "Z"),
                "createdAt": days_ago.isoformat().replace("+00:00", "Z"),
                "headRefName": "m-chore-x-RBT-723", "repository": {"nameWithOwner": "o/r"},
                "author": {"login": "me"}, "labels": {"nodes": []},
                "closingIssuesReferences": {"nodes": []}}
        cli, errors = Fake(prs=[node]), []
        closed = attention._collect_closed(cli, "me", NOW, errors)
        self.assertEqual(errors, [])
        self.assertEqual([(i["ref"], i["jira"].rsplit("/", 1)[-1]) for i in closed],
                         [("r#59", "RBT-723")])
        since = (NOW - timedelta(hours=attention.CLOSED_MEMORY_HOURS)).strftime("%Y-%m-%d")
        self.assertEqual(attention.CLOSED_MEMORY_HOURS, attention.GONE_WINDOW_HOURS)
        self.assertIn(f"closed:>={since}", cli.queries[0])

    def test_all_closed_cluster_stays_in_the_closed_log(self):
        closed = [
            attention._item("ISSUE", container="o/r", ref="r#2", states=["closed"],
                            times={"updated": "2026-09-29T09:01:00Z"}, links=["o/r#5"]),
            attention._item("MY PR", container="o/r", ref="r#5", states=["merged"],
                            times={"updated": "2026-09-29T09:00:00Z"}, links=["o/r#2"]),
        ]
        view = attention.build_view([], now=NOW, closed=closed)
        self.assertEqual([t["items"] for t in view["tiers"]], [[], [], []])
        self.assertEqual(len(view["closed"]), 1)
        self.assertEqual(view["closed"][0]["ref"], "r#5")
        self.assertEqual([c["ref"] for c in view["closed"][0]["children"]], ["r#2"])

    def test_a_live_ticket_heads_the_card_its_merged_pr_helped(self):
        """Finished work does not name a card that still has live work: the
        ticket heads it, the merged PR rides struck under it, and the card sits
        in the ticket's own tier instead of being lifted to Ready by a merge."""
        ticket = attention._item("JIRA", source="jira", ref="FIRE-1", states=["in-progress"],
                                 times={"updated": "2026-09-29T09:00:00Z"})
        merged = attention._item("MY PR", container="o/r", ref="r#7", states=["merged"],
                                 times={"updated": "2026-09-29T09:30:00Z"},
                                 jira=attention.JIRA_BASE + "FIRE-1")
        view = attention.build_view([ticket], now=NOW, closed=[merged])
        cards = [i for t in view["tiers"] for i in t["items"]]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["tier"], "waiting")
        self.assertEqual(cards[0]["ref"], "FIRE-1")
        self.assertEqual([c["ref"] for c in cards[0]["children"]], ["r#7"])
        self.assertEqual(cards[0]["children"][0]["states"][0]["key"], "merged")
        self.assertEqual(cards[0]["jira"], "")   # the ticket is the card, not a line under it
        self.assertEqual(view["closed"], [])

    def test_closed_bucket_is_newest_first_and_not_in_tiers(self):
        open_items = [attention._item("MY PR", container="o/r", ref="r#1", states=["ready"])]
        closed = [attention._item("MY PR", container="o/r", ref="r#2", states=["merged"],
                                  times={"updated": "2026-09-29T11:00:00Z"}),
                  attention._item("ISSUE", container="o/r", ref="r#3", states=["closed"],
                                  times={"updated": "2026-09-29T05:00:00Z"})]
        view = attention.build_view(open_items, now=NOW, closed=closed)
        self.assertEqual([r["ref"] for r in view["closed"]], ["r#2", "r#3"])
        self.assertEqual([r["ref"] for t in view["tiers"] for r in t["items"]], ["r#1"])
        self.assertEqual([s["key"] for s in view["closed"][0]["states"]], ["merged"])
        self.assertEqual(view["closed"][0]["age"], "1h")


class CheckParsing(unittest.TestCase):
    """The checks arrive inside the PR read now, one rollup context each, so the
    only thing left to get wrong is the mapping to the {name, state} the states
    have always read."""

    ROLLUP = {"statusCheckRollup": {"contexts": {"nodes": [
        {"__typename": "CheckRun", "name": "unit tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "name": "integration", "status": "IN_PROGRESS", "conclusion": None},
        {"__typename": "StatusContext", "context": "ci/legacy", "state": "FAILURE"},
    ]}}}

    def test_a_finished_run_reports_its_conclusion_and_a_live_one_its_status(self):
        self.assertEqual(attention._checks_of(self.ROLLUP), [
            {"name": "unit tests", "state": "SUCCESS"},
            {"name": "integration", "state": "IN_PROGRESS"},
            {"name": "ci/legacy", "state": "FAILURE"},
        ])

    def test_the_states_read_them_the_way_gh_pr_checks_did(self):
        checks = attention._checks_of(self.ROLLUP)
        self.assertEqual([attention._check_state(c) for c in checks], ["green", "pending", "failed"])
        self.assertEqual(attention._pr_facts({}, checks)[1], {"label": "1 checks failing", "tone": "bad"})
        self.assertEqual(attention._pr_facts({}, [])[1], {"label": "no checks", "tone": "warn"})

    def test_no_checks_is_no_checks(self):
        for rollup in ({}, {"statusCheckRollup": None}, {"statusCheckRollup": {"contexts": {"nodes": []}}}):
            self.assertEqual(attention._checks_of(rollup), [], rollup)


class AssignedIssues(unittest.TestCase):
    def test_331_waiting_reply_only(self):
        view, _ = build()
        row = find(view, "checkout-api#331")
        self.assertEqual([s["key"] for s in row["states"]], ["waiting-reply"])
        self.assertEqual(row["labels"], [{"name": "spec", "color": "7057FF"}])

    def test_332_waiting_reply_and_in_progress_with_linked_pr(self):
        view, _ = build()
        row = find(view, "checkout-api#332")
        self.assertEqual([s["key"] for s in row["states"]], ["waiting-reply", "in-progress"])
        self.assertEqual(row["detail"], "1 comment · PR #335")
        self.assertEqual(row["labels"], [{"name": "ticket", "color": "C2E0C6"}])

    def test_issue_labels_are_normalized(self):
        items = attention.items_from_issues(FIXTURES["search_assignee"], FIXTURES["issue_view"],
                                      FIXTURES["issue_linked_prs"], ME)
        labels = {i["ref"]: i["labels"] for i in items}
        self.assertEqual(labels["checkout-api#331"], [{"name": "spec", "color": "7057FF"}])
        self.assertEqual(labels["checkout-api#332"], [{"name": "ticket", "color": "C2E0C6"}])

    def test_issue_body_jira_spec_links_the_cards(self):
        view = {"number": 2330, "title": "Drop stories from the final files",
                "url": "https://github.com/acme/payments-api/issues/2330",
                "body": "## Parent\n\nJira spec: https://launchmetrics.atlassian.net/browse/RBT-9003\n",
                "comments": [], "labels": [],
                "updatedAt": "2026-09-29T10:00:00Z", "createdAt": "2026-09-29T09:00:00Z"}
        item = attention._issue_item(view, ["not-started"], [], {})
        self.assertEqual(item["jira"], "https://launchmetrics.atlassian.net/browse/RBT-9003")
        jira = attention._item("JIRA", source="jira", ref="RBT-9003", states=["waiting"])
        built = attention.build_view([item, jira], now=NOW)
        # one card, headed by the Jira ticket; the issue rides under it.
        self.assertEqual([r["ref"] for r in built["tiers"][0]["items"]], ["RBT-9003"])
        self.assertEqual([c["ref"] for c in built["tiers"][0]["items"][0]["children"]],
                         ["payments-api#2330"])

    def test_parent_hash_in_the_body_joins_the_tickets_to_the_spec(self):
        """`## Parent` / `#1338` is the link GitHub renders. The four tickets
        and the spec they name are one card, not five."""
        def issue(num, body):
            return {"number": num, "title": f"t{num}",
                    "url": f"https://github.com/acme/billing-service/issues/{num}",
                    "body": body, "comments": [], "labels": [],
                    "updatedAt": "2026-10-08T12:00:00Z", "createdAt": "2026-10-08T11:00:00Z"}
        spec = issue(1338, "## Problem\n\nno children listed\n")
        tickets = [issue(n, "## Parent\n\n#1338\n") for n in (1339, 1340, 1341, 1342)]
        items = [attention._issue_item(spec, ["waiting-reply"], [], {})]
        items += [attention._issue_item(v, ["not-started"], [], {}) for v in tickets]
        view = attention.build_view(items, now=NOW)
        cards = [i for t in view["tiers"] for i in t["items"]]
        self.assertEqual(len(cards), 1)
        refs = {cards[0]["ref"]} | {c["ref"] for c in cards[0]["children"]}
        self.assertEqual(refs, {f"billing-service#{n}" for n in range(1338, 1343)})

    def test_header_is_jira_then_spec_then_ticket_then_pr(self):
        spec = attention._item("ISSUE", container="o/r", ref="r#1", states=["waiting-reply"],
                               labels=[{"name": "spec"}], links=["RBT-1"])
        ticket = attention._item("ISSUE", container="o/r", ref="r#2", states=["not-started"],
                                 labels=[{"name": "ticket"}], links=["o/r#1"])
        pr = attention._item("MY PR", container="o/r", ref="r#3", states=["ready"],
                             links=["o/r#2"])
        jira = attention._item("JIRA", source="jira", ref="RBT-1", states=["in-progress"])
        view = attention.build_view([pr, ticket, spec, jira], now=NOW)
        card = [i for t in view["tiers"] for i in t["items"]][0]
        self.assertEqual(card["ref"], "RBT-1")
        self.assertEqual([c["ref"] for c in card["children"]], ["r#1", "r#2", "r#3"])
        # no Jira: the spec names the card, ahead of the ticket and the PR
        view = attention.build_view([pr, ticket, spec], now=NOW)
        card = [i for t in view["tiers"] for i in t["items"]][0]
        self.assertEqual(card["ref"], "r#1")
        self.assertEqual([c["ref"] for c in card["children"]], ["r#2", "r#3"])

    def test_needs_reply_and_not_started(self):
        states, prs = attention.issue_states({"comments": [{"author": {"login": "someone"}}]}, {}, ME)
        self.assertEqual(states, ["needs-reply"])
        states, prs = attention.issue_states({"comments": []}, {}, ME)
        self.assertEqual(states, ["not-started"])
        view = attention.build_view([attention._item("", ref="r#1", states=states)], now=NOW)
        self.assertEqual(view["tiers"][0]["items"][0]["ref"], "r#1")


class OrderingAndErrors(unittest.TestCase):
    def test_stalest_member_is_header_across_time_offsets(self):
        items = [
            attention._item("ISSUE", container="o/r", ref="r#2330", states=["waiting-reply"],
                            times={"updated": "2026-09-29T12:58:59Z"}),
            attention._item("JIRA", source="jira", ref="RBT-9003", states=["in-progress"],
                            times={"updated": "2026-09-29T14:00:47.547+02:00"}, links=["o/r#2330"]),
        ]
        view = attention.build_view(items, now=NOW)
        self.assertEqual([r["ref"] for r in view["tiers"][2]["items"]], ["RBT-9003"])
        self.assertEqual([c["ref"] for c in find(view, "RBT-9003")["children"]], ["r#2330"])

    def test_tier_order_is_chronological_across_time_offsets(self):
        items = [
            attention._item("", ref="newer", states=["waiting"],
                            times={"updated": "2026-09-29T12:58:59Z"}),
            attention._item("", ref="older", states=["waiting"],
                            times={"updated": "2026-09-29T14:00:47.547+02:00"}),
        ]
        view = attention.build_view(items, now=NOW)
        self.assertEqual([r["ref"] for r in view["tiers"][2]["items"]], ["older", "newer"])

    def test_stalest_activity_first_within_tier(self):
        items = [
            attention._item("", ref="a", states=["review-requested"],
                            times={"updated": "2026-09-29T10:00:00Z"}),
            attention._item("", ref="b", states=["review-requested"],
                            times={"updated": "2026-09-20T10:00:00Z"}),
            attention._item("", ref="c", states=["review-requested"],
                            times={"updated": "2026-09-25T10:00:00Z"}),
        ]
        view = attention.build_view(items, now=NOW)
        self.assertEqual([r["ref"] for r in view["tiers"][0]["items"]], ["b", "c", "a"])

    def test_errors_pass_through_never_empty_silently(self):
        err = {"where": "review requests", "command": "gh search prs", "output": "boom"}
        view = attention.build_view([], errors=[err], now=NOW)
        self.assertEqual(view["errors"], [err])

    def test_issue_refs_in_skips_cross_repo_refs(self):
        self.assertEqual(attention._issue_refs_in("feat: #2330 Drop stories", "o/r"), ["o/r#2330"])
        self.assertEqual(attention._issue_refs_in("see acme/shared-lib#409", "o/r"), [])
        self.assertEqual(attention._issue_refs_in(None, "o/r"), [])

    def test_jira_url_in_only_matches_atlassian_browse_links(self):
        self.assertEqual(attention._jira_url_in("Jira spec: https://launchmetrics.atlassian.net/browse/RBT-9003 (UTF-8 ok)"),
                         "https://launchmetrics.atlassian.net/browse/RBT-9003")
        self.assertEqual(attention._jira_url_in("mentions UTF-8 and RBT-9003 as plain text"), "")
        self.assertEqual(attention._jira_url_in("https://example.com/browse/RBT-9003"), "")
        self.assertEqual(attention._jira_url_in(None), "")

    def test_jira_url_only_matches_uppercase_key(self):
        self.assertEqual(attention.jira_url("m-chore-bump_lm_data_unification_3_9_6-FIRE-9001"),
                         "https://launchmetrics.atlassian.net/browse/FIRE-9001")
        self.assertEqual(attention.jira_url("fix-123-something"), "")
        self.assertEqual(attention.jira_url("bb/orchestrate-bb-plan-https-github-com-launchmetri"), "")
        self.assertEqual(attention.jira_url(""), "")
        self.assertEqual(attention.jira_url(None), "")

    def test_humanize_age(self):
        self.assertEqual(attention.humanize_age("2026-09-29T11:30:00Z", NOW), "30m")
        self.assertEqual(attention.humanize_age("2026-09-29T05:00:00Z", NOW), "7h")
        self.assertEqual(attention.humanize_age("2026-09-24T12:00:00Z", NOW), "5d")
        self.assertEqual(attention.humanize_age("2026-07-24T12:00:00Z", NOW), "2mo")


class OpenSessions(unittest.TestCase):
    """Sessions and specs are read per request, never snapshotted: a pane is open
    now or it is not, and a spec waits for nobody. Both are joined to the board by
    repository, so what a session says about itself is the whole contract."""

    class Fake:
        """Only what these two reads touch: herdr panes, bb threads and projects,
        git remotes, and gh's own issue search."""
        def __init__(self, panes=(), threads=(), projects=(), remotes=None, issues=None,
                     issues_by_repo=None, fail=(), fail_repos=(), absent=()):
            self.panes, self.threads, self.projects = list(panes), list(threads), list(projects)
            self.remotes, self.issues, self.fail, self.calls = remotes or {}, list(issues or []), set(fail), []
            # a watched-repo search answers for its own repo, and can fail alone
            self.issues_by_repo, self.fail_repos = dict(issues_by_repo or {}), set(fail_repos)
            self.absent = set(absent)          # a CLI this machine does not have

        def json(self, command, args):
            self.calls.append([command] + list(args))
            if command in self.absent:
                raise FileNotFoundError(2, "No such file or directory", command)
            if command in self.fail:
                raise attention.CliError(command, "boom")
            if command == "herdr":
                return {"result": {"panes": self.panes}}
            if command == "bb":
                return self.projects if args[0] == "project" else self.threads
            if command == "gh":
                repo = args[args.index("--repo") + 1] if "--repo" in args else None
                if repo in self.fail_repos:
                    raise attention.CliError(command, "boom")
                return self.issues_by_repo.get(repo, []) if repo else self.issues
            raise AssertionError(command)

        def text(self, command, args):
            self.calls.append([command] + list(args))
            if command in ("herdr", "bb", "open"):   # the focus action itself
                return ""
            url = self.remotes.get(args[1], "")
            if not url:
                raise attention.CliError(command, "no such remote")
            return url + "\n"

    def pane(self, **over):
        pane = {"pane_id": "wN:p1", "tab_id": "wN:t1", "workspace_id": "wN", "cwd": "/w/shared-lib",
                "agent": "pi", "agent_status": "working", "terminal_title_stripped": "\u03c0 - shared-lib"}
        pane.update(over)
        return pane

    def spec(self, number, repo, author="", assignees=()):
        """One gh search-issue row, in the shape the specs read asks for."""
        return {"number": number, "title": f"spec {number}",
                "url": f"https://github.com/{repo}/issues/{number}",
                "updatedAt": "2026-10-06T15:00:00Z", "assignees": list(assignees),
                "author": {"login": author} if author else None,
                "repository": {"nameWithOwner": repo}}

    def test_a_teammate_read_uses_their_login_and_skips_only_what_needs_it(self):
        seen = []

        class Rec:
            def text(self, command, args):
                seen.append([command] + list(args))
                return "{}"

        cli = attention.AsThem("teammate-one", account_id="abc", inner=Rec())
        self.assertEqual(cli.text("gh", ["search", "issues", "--assignee=@me"]), "{}")
        self.assertEqual(seen[-1], ["gh", "search", "issues", "--assignee=teammate-one"])
        self.assertEqual(cli.text("twg", ["jira", "query", "assignee = currentUser()"]), "{}")
        self.assertEqual(seen[-1][-1], 'assignee = "abc"')

        # No account to read as: the who-am-I sources stand down, but a read by
        # key is the same ticket for anyone, so it still goes through.
        theirs = attention.AsThem("teammate-two", inner=Rec())
        self.assertEqual(theirs.text("twg", ["jira", "workitem", "get", "RBT-949"]), "{}")
        self.assertEqual(seen[-1], ["twg", "jira", "workitem", "get", "RBT-949"])
        with self.assertRaises(attention.CliError) as caught:
            theirs.text("twg", ["jira", "workitem", "query", "--jql", "assignee = currentUser()"])
        # "skipped" is the word their_queue drops: a source that is off for
        # them, not a queue that broke.
        self.assertIn("skipped", caught.exception.output)

    def test_a_tool_this_machine_lacks_is_not_a_failure(self):
        """A missing CLI is a source that is off here: named for the work sources,
        and simply nothing to show for the session rails — herdr and bb are this
        machine's own readers, so a machine without them has no sessions."""
        gone = attention.live_sessions(cli=self.Fake(absent=("herdr", "bb")))
        self.assertEqual((gone["repos"], gone["errors"]), ({}, []))
        broken = attention.live_sessions(cli=self.Fake(fail=("herdr", "bb")))
        self.assertEqual([e["where"] for e in broken["errors"]],
                         ["herdr sessions", "bb sessions"])   # a real break still says so

        absent = attention._error("jira tasks", FileNotFoundError(2, "No such file or directory", "twg"))
        self.assertEqual(absent["missing"], "twg")
        self.assertNotIn("missing", attention._error("jira tasks", attention.CliError("twg x", "auth expired")))
        self.assertNotIn("missing", attention._error("jira tasks", ValueError("bad json")))

    def test_a_pane_is_a_session_per_repo_with_its_own_tab(self):
        cli = self.Fake(panes=[self.pane()],
                        remotes={"/w/shared-lib": "git@github.com:acme/shared-lib.git"})
        out = attention.live_sessions(cli=cli)
        self.assertEqual(list(out["repos"]), ["acme/shared-lib"])
        session = out["repos"]["acme/shared-lib"][0]
        self.assertEqual(session["origin"], "herdr")
        self.assertTrue(session["busy"])
        self.assertEqual(session["herdr"], {"tab": "wN:t1", "workspace": "wN"})
        self.assertIsNone(session["bb"])
        self.assertEqual(out["errors"], [])

    def test_a_pane_inside_a_bb_worktree_opens_both(self):
        """herdr reports no agent for it, but the path names the thread: the same
        agent with a different boss, and the card may go to either one."""
        path = "/Users/a/.bb/plugins/environment-git-worktree/host-data/worktrees/thr_sa8ywf5fsx-1/checkout-api"
        cli = self.Fake(panes=[self.pane(agent=None, agent_status=None, cwd=path, pane_id="w4:p3",
                                         tab_id="w4:t3", workspace_id="w4")],
                        remotes={path: "git@github.com:acme/checkout-api.git"})
        session = attention.live_sessions(cli=cli)["repos"]["acme/checkout-api"][0]
        self.assertEqual((session["agent"], session["state"]), ("bb", "shell"))
        self.assertEqual(session["origin"], "bb")
        self.assertFalse(session["busy"])
        self.assertEqual(session["bb"], {"thread": "thr_sa8ywf5fsx"})
        self.assertEqual(session["herdr"]["tab"], "w4:t3")

    def test_a_plain_shell_and_an_idle_thread_are_not_sessions(self):
        cli = self.Fake(panes=[self.pane(agent=None, agent_status=None)],
                        threads=[{"id": "thr_x", "status": "idle", "projectId": "proj_1"}],
                        projects=[{"id": "proj_1", "sources": [{"path": "/w/shared-lib"}]}],
                        remotes={"/w/shared-lib": "git@github.com:acme/shared-lib.git"})
        self.assertEqual(attention.live_sessions(cli=cli)["repos"], {})

    def test_an_active_thread_is_a_session_even_with_no_pane(self):
        """A bb thread can run on another machine, in a checkout with no herdr
        pane: the thread record is the only evidence there is."""
        cli = self.Fake(threads=[{"id": "thr_dxmwv5t789", "status": "active", "providerId": "pi",
                                  "title": "", "projectId": "proj_1"}],
                        projects=[{"id": "proj_1", "sources": [{"path": "/w/billing-service"}]}],
                        remotes={"/w/billing-service": "git@github.com:acme/billing-service.git"})
        session = attention.live_sessions(cli=cli)["repos"]["acme/billing-service"][0]
        self.assertEqual((session["origin"], session["state"], session["busy"]), ("bb", "active", True))
        self.assertEqual(session["bb"], {"thread": "thr_dxmwv5t789"})
        self.assertIsNone(session["herdr"])

    def test_a_failed_read_is_reported_not_raised(self):
        out = attention.live_sessions(cli=self.Fake(fail=("herdr",)))
        self.assertEqual(out["repos"], {})
        self.assertEqual([e["where"] for e in out["errors"]], ["herdr sessions"])

    def test_a_failed_jump_says_which_command_failed(self):
        class Stubborn(self.Fake):
            def text(self, command, args):
                self.calls.append([command] + list(args))
                raise attention.CliError(command, "no such thread")
        out = attention.focus({"kind": "bb", "target": "thr_x"}, cli=Stubborn())
        self.assertFalse(out["ok"])
        self.assertEqual(out["ran"], "bb thread open thr_x")   # the raise never ran
        self.assertEqual(out["error"], "no such thread")

    def test_specs_are_the_unassigned_issues_i_wrote_with_that_label(self):
        """An assignee means someone is already on it — usually me, which is why
        those are already on the board — so a taken spec is not groundwork."""
        cli = self.Fake(issues=[
            {"number": 1338, "title": "App releases follow the company CI/CD standard",
             "url": "https://github.com/acme/billing-service/issues/1338",
             "updatedAt": "2026-10-06T15:46:30Z", "assignees": [],
             "author": {"login": "albertvila"},
             "repository": {"nameWithOwner": "acme/billing-service"}},
            {"number": 413, "title": "Read consolidated docs once",
             "url": "https://github.com/acme/shared-lib/issues/413",
             "updatedAt": "2026-10-06T15:10:09Z", "assignees": [{"login": "albertvila"}],
             "repository": {"nameWithOwner": "acme/shared-lib"}}])
        issues = attention.spec_issues(cli=cli, config={})["issues"]
        self.assertEqual([i["ref"] for i in issues], ["acme/billing-service#1338"])
        self.assertEqual(issues[0]["repo"], "acme/billing-service")
        self.assertEqual(issues[0]["updated"], "2026-10-06T15:46:30Z")
        self.assertEqual(issues[0]["author"], "albertvila")     # the rail draws their face
        self.assertFalse(issues[0]["watched"])                # read as mine, so no second list
        self.assertIn("--label=spec", cli.calls[0])
        self.assertIn("assignees", cli.calls[0][-1])      # the read asks who took it
        failed = attention.spec_issues(cli=self.Fake(fail=("gh",)), config={})
        self.assertEqual((failed["issues"], [e["where"] for e in failed["errors"]]), ([], ["spec issues"]))

    def test_a_watched_repo_specs_are_read_whoever_wrote_them(self):
        """config.json's specRepos: a teammate's proposal is groundwork I may want
        to read before it is taken — in the repos I named, and only there. Each row
        says which read it came from, so a rail can keep the two apart."""
        cli = self.Fake(issues=[self.spec(10, "acme/shared-lib", author="albertvila")],
                        issues_by_repo={"acme/checkout-api":
                                        [self.spec(7, "acme/checkout-api", author="djo19")]})
        out = attention.spec_issues(cli=cli, config={"specRepos": ["acme/checkout-api"]})
        self.assertEqual([i["ref"] for i in out["issues"]],
                         ["acme/shared-lib#10", "acme/checkout-api#7"])
        self.assertEqual([i["author"] for i in out["issues"]], ["albertvila", "djo19"])
        self.assertEqual([i["watched"] for i in out["issues"]], [False, True])
        # The two searches run at once, so which answers first is not the test:
        # each asks for what it should, and mine still wins the merge above.
        by_me = next(c for c in cli.calls if "--author=@me" in c)
        by_repo = next(c for c in cli.calls if "--repo" in c)
        self.assertNotIn("--author=@me", by_repo)
        self.assertEqual(by_repo[by_repo.index("--repo") + 1], "acme/checkout-api")
        self.assertEqual(out["errors"], [])

    def test_a_watched_repo_never_lists_the_same_spec_twice(self):
        """Mine is read first and wins, so it stays in the mine list."""
        spec = self.spec(10, "o/r", author="me")
        cli = self.Fake(issues=[spec], issues_by_repo={"o/r": [spec]})
        out = attention.spec_issues(cli=cli, config={"specRepos": ["o/r"]})
        self.assertEqual([i["ref"] for i in out["issues"]], ["o/r#10"])
        self.assertEqual((out["issues"][0]["author"], out["issues"][0]["watched"]), ("me", False))

    def test_one_failing_watched_repo_costs_only_itself(self):
        cli = self.Fake(issues=[self.spec(10, "o/mine")], fail_repos=("o/broken",))
        out = attention.spec_issues(cli=cli, config={"specRepos": ["o/broken"]})
        self.assertEqual([i["ref"] for i in out["issues"]], ["o/mine#10"])
        self.assertEqual([e["where"] for e in out["errors"]], ["spec issues (o/broken)"])

    def test_a_config_that_says_nothing_readable_is_the_defaults(self):
        """A stray string is none of them, not a search per character."""
        self.assertEqual(attention.spec_repos({}), [])
        self.assertEqual(attention.spec_repos({"specRepos": "all"}), [])
        self.assertEqual(attention.spec_repos({"specRepos": ["o/r", 7, None]}), ["o/r"])
        self.assertEqual(attention.load_config("/nonexistent/config.json"), {})
        self.assertEqual(attention.stalk_teams({}), [])
        self.assertEqual(attention.stalk_teams({"stalkTeams": "squad-platform"}), [])
        self.assertEqual(attention.stalk_teams(
            {"stalkTeams": ["squad-platform", "../evil", 3, "team-payments"]}),
            ["squad-platform", "team-payments"])

    def test_the_roster_leaves_me_out_and_the_you_row_carries_my_login(self):
        """You are the "You" row, so the roster below is everyone else — a roster
        that names you twice is a roster you stop reading. My login rides along
        because the avatar on that row has to come from somewhere."""
        listing = [{"login": "albertvila"}, {"login": "teammate-one"}, {"login": "lm-sec-github"}]

        class Live:
            def json(self, command, args):
                if "teams/" in args[1]:
                    return listing
                return {"name": "Ada Lovelace"}

        with mock.patch.object(attention, "LIVE", Live()):
            out = attention.team_members(config={"stalkTeams": ["squad-platform"]}, me="albertvila")
        self.assertEqual(out["you"], {"login": "albertvila"})
        self.assertEqual([p["login"] for p in out["people"]], ["teammate-one"])     # bots and me left out
        self.assertEqual(out["people"][0]["name"], "Ada Lovelace")

    def test_the_stalker_switch_closes_the_teammate_queue(self):
        """`stalker: false` is the whole feature off: no faces to click and no
        queue read behind one, without touching the team list itself."""
        teams = {"stalkTeams": ["squad-platform"]}
        self.assertTrue(attention.stalker_on({}))          # absent is on
        self.assertTrue(attention.stalker_on(dict(teams, stalker=True)))
        self.assertTrue(attention.stalker_on({"stalker": "no"}))
        off = dict(teams, stalker=False)
        self.assertFalse(attention.stalker_on(off))
        self.assertEqual(attention.team_members(config=off), {"you": {}, "people": []})
        self.assertEqual(attention.their_queue("teammate-one", config=off),
                         {"error": "stalker is off"})

    def test_focus_runs_only_the_commands_a_session_needs_and_only_for_an_id(self):
        cli = self.Fake()
        # both kinds focus the session and then raise the app: `herdr tab focus`
        # moves Herdr's own focus without bringing the window forward, so a reader
        # in a system browser would otherwise see nothing happen
        self.assertEqual(attention.focus({"kind": "herdr", "target": "wN:t1"}, cli=cli),
                         {"ok": True, "ran": "herdr tab focus wN:t1 && open -a Herdr", "raised": True})
        self.assertEqual(cli.calls[-2:], [["herdr", "tab", "focus", "wN:t1"], ["open", "-a", "Herdr"]])
        self.assertEqual(attention.focus({"kind": "bb", "target": "thr_sa8ywf5fsx"}, cli=cli),
                         {"ok": True, "ran": "bb thread open thr_sa8ywf5fsx && open -a bb", "raised": True})
        self.assertEqual(cli.calls[-2:], [["bb", "thread", "open", "thr_sa8ywf5fsx"], ["open", "-a", "bb"]])
        for bad in ({"kind": "herdr", "target": "wN:t1; whoami"}, {"kind": "herdr", "target": ""},
                    {"kind": "shell", "target": "wN:t1"}, {}):
            self.assertFalse(attention.focus(bad, cli=cli)["ok"], bad)
        self.assertEqual(len(cli.calls), 4)          # nothing else ever reached the CLI

    def test_a_window_that_will_not_come_forward_is_not_a_failed_jump(self):
        """The jump is the focus; raising the window is a nicety, and `open` is not
        everywhere. A raise that fails still leaves the session focused, and says so."""
        class NoWindow(self.Fake):
            def text(self, command, args):
                if command == "open":
                    raise attention.CliError(command, "open: command not found")
                self.calls.append([command] + list(args))
        out = attention.focus({"kind": "herdr", "target": "wN:t1"}, cli=NoWindow())
        self.assertTrue(out["ok"])
        self.assertFalse(out["raised"])
        self.assertEqual(out["ran"], "herdr tab focus wN:t1 && open -a Herdr")


class Snoozes(unittest.TestCase):
    """Snooze is user intent in its own file: nothing about the snapshot moves."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="snooze-test-")
        self.path = os.path.join(self.dir, "snoozes.json")

    def test_add_expiry_and_prune_on_write(self):
        soon = attention._iso_utc(NOW + timedelta(hours=1))
        self.assertEqual(attention.write_snooze("o/r#1", soon, path=self.path, now=NOW), {"o/r#1": soon})
        self.assertEqual(attention.load_snoozes(self.path, now=NOW), {"o/r#1": soon})
        # an expired key disappears from the read, and from the file on the next write
        later = NOW + timedelta(hours=2)
        self.assertEqual(attention.load_snoozes(self.path, now=later), {})
        later_still = attention._iso_utc(NOW + timedelta(hours=4))
        self.assertEqual(attention.write_snooze("o/r#2", later_still, path=self.path, now=later),
                         {"o/r#2": later_still})

    def test_wake_removes_the_key(self):
        until = attention._iso_utc(NOW + timedelta(hours=1))
        attention.write_snooze("o/r#1", until, path=self.path, now=NOW)
        attention.write_snooze("o/r#2", until, path=self.path, now=NOW)
        self.assertEqual(attention.write_snooze("o/r#1", "", path=self.path, now=NOW), {"o/r#2": until})

    def test_missing_or_broken_file_is_just_empty(self):
        self.assertEqual(attention.load_snoozes(self.path), {})
        with open(self.path, "w") as fh:
            fh.write("not json")
        self.assertEqual(attention.load_snoozes(self.path), {})

    def test_snooze_request_hours_and_wake(self):
        path = attention.snooze_path
        attention.snooze_path = lambda: self.path
        try:
            data = attention.snooze({"key": "o/r#1", "hours": 4})
            self.assertEqual(list(data), ["o/r#1"])
            self.assertEqual(attention.snooze({"key": "o/r#1", "hours": 0}), {})
            self.assertEqual(attention.snooze({}), {"error": "key is required"})
        finally:
            attention.snooze_path = path


class TheWriters(unittest.TestCase):
    """Every write lands whole: the server and a cron can write the same file,
    and a reader that catches half of one loses what was in it."""

    def test_a_reader_never_catches_half_a_file(self):
        directory = tempfile.mkdtemp(prefix="atomic-test-")
        path = os.path.join(directory, "attention.json")
        stop = threading.Event()

        def writer():
            while not stop.is_set():
                attention.write_snapshot({"filler": "x" * 5000}, path)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            for _ in range(300):
                try:
                    with open(path) as fh:
                        json.load(fh)      # a torn file is not JSON and raises here
                except FileNotFoundError:
                    pass                   # before the first write has landed
        finally:
            stop.set()
            thread.join()
        self.assertEqual(sorted(os.listdir(directory)), ["attention.json"])


class SnapshotContract(unittest.TestCase):
    """The shared JSON: stable keys, raw times, and the change block."""

    def item(self, num, state, title=None):
        return attention._item("", container="o/r", ref=f"o/r#{num}", title=title or f"pr {num}",
                               url=f"https://x/{num}", states=[state],
                               times={"updated": "2026-09-28T12:00:00Z"})

    def view(self, items):
        return attention.build_view(items, now=NOW)

    def test_rows_carry_key_times_and_tone(self):
        view = self.view([dict(self.item(1, "needs-comments"), links=["o/r#2"])])
        row = find(view, "o/r#1")
        self.assertEqual(row["key"], "o/r#1")
        self.assertEqual(row["links"], ["o/r#2"])       # why this card clustered
        self.assertEqual(row["times"], {"updated": "2026-09-28T12:00:00Z"})
        self.assertEqual(row["states"][0]["tone"], "warn")

    def test_payload_is_one_json_document(self):
        snapshot = attention.payload(self.view([self.item(1, "ready")]), now=NOW)
        self.assertEqual(snapshot["schema"], attention.SCHEMA_VERSION)
        self.assertEqual(snapshot["generatedAt"], "2026-09-29T12:00:00Z")
        self.assertIsNone(snapshot["changes"]["previousAt"])
        self.assertEqual(json.loads(json.dumps(snapshot))["tiers"], snapshot["tiers"])

    def test_changes_new_moved_and_gone(self):
        before = attention.payload(self.view([
            self.item(1, "waiting"), self.item(2, "ready"), self.item(3, "waiting")]), now=NOW)
        after = attention.payload(self.view([
            self.item(1, "waiting"), self.item(2, "ci-failing"), self.item(4, "ready")]),
            previous=before, now=NOW)
        changes = after["changes"]
        self.assertEqual(changes["previousAt"], "2026-09-29T12:00:00Z")
        self.assertEqual(changes["summary"], {"new": 1, "moved": 1, "gone": 1})
        self.assertEqual(changes["items"]["o/r#2"],
                         {"kind": "moved", "from_state": "ready", "to_state": "ci-failing",
                          "from_label": "ready", "to_label": "CI failing",
                          "from_tier": "ready", "to_tier": "needs",
                          "label": "ready → CI failing"})
        self.assertEqual(changes["items"]["o/r#4"], {"kind": "new"})
        self.assertNotIn("o/r#1", changes["items"])
        self.assertEqual([(r["key"], r["section"], r["change"]["kind"]) for r in changes["gone"]],
                         [("o/r#3", "waiting", "gone")])
        self.assertEqual(find(after, "o/r#2")["change"]["kind"], "moved")
        self.assertNotIn("change", find(after, "o/r#1"))

    def test_second_state_moving_a_row_labels_the_tier_move(self):
        def it(states):
            return attention._item("", container="o/r", ref="o/r#1", states=states,
                                   times={"updated": "2026-09-28T12:00:00Z"})
        before = attention.payload(self.view([it(["waiting-reply"])]), now=NOW)
        after = attention.payload(self.view([it(["waiting-reply", "conflicts"])]), previous=before, now=NOW)
        self.assertEqual(after["changes"]["items"]["o/r#1"]["label"],
                         "Waiting on others → Needs you now")

    def test_first_seen_and_last_change_survive_generations(self):
        first = attention.payload(self.view([self.item(1, "ready"), self.item(2, "waiting")]), now=NOW)
        later = NOW + timedelta(hours=2)
        second = attention.payload(self.view([self.item(1, "ci-failing"), self.item(2, "waiting"),
                                        self.item(3, "ready")]), previous=first, now=later)
        rows = {r["key"]: r for t in second["tiers"] for r in t["items"]}
        # unchanged row: no freshness for this generation, but its sighting stays
        self.assertEqual(rows["o/r#2"]["firstSeenAt"], "2026-09-29T12:00:00Z")
        self.assertEqual(rows["o/r#2"]["lastChangedAt"], "2026-09-29T12:00:00Z")
        self.assertNotIn("change", rows["o/r#2"])
        # moved row: this generation's change AND the durable last change
        self.assertEqual(rows["o/r#1"]["lastChangedAt"], "2026-09-29T14:00:00Z")
        self.assertEqual(rows["o/r#1"]["lastChange"]["label"], "ready → CI failing")
        # the move stays visible on the next generation even though `change` is gone
        third = attention.payload(self.view([self.item(1, "ci-failing"), self.item(2, "waiting"),
                                       self.item(3, "ready")]), previous=second,
                            now=NOW + timedelta(hours=4))
        moved = {r["key"]: r for t in third["tiers"] for r in t["items"]}["o/r#1"]
        self.assertNotIn("change", moved)
        self.assertEqual(moved["lastChange"]["label"], "ready → CI failing")
        self.assertEqual(moved["lastChangedAt"], "2026-09-29T14:00:00Z")

    def test_gone_rows_persist_for_a_later_look(self):
        first = attention.payload(self.view([self.item(1, "waiting"), self.item(2, "waiting")]), now=NOW)
        second = attention.payload(self.view([self.item(1, "waiting")]), previous=first, now=NOW)
        third = attention.payload(self.view([self.item(1, "waiting")]), previous=second,
                            now=NOW + timedelta(hours=1))
        self.assertEqual(second["changes"]["summary"]["gone"], 1)
        self.assertEqual(third["changes"]["summary"]["gone"], 0)
        self.assertEqual([(r["key"], r["goneAt"]) for r in third["changes"]["gone"]],
                         [("o/r#2", "2026-09-29T12:00:00Z")])

    def test_change_annotation_survives_leaving_the_tiers(self):
        before = attention.payload(self.view([self.item(1, "needs-comments")]), now=NOW)
        merged = attention._item("MY PR", container="o/r", ref="o/r#1", title="pr 1",
                                 url="https://x/1", states=["merged"],
                                 times={"updated": "2026-09-28T12:00:00Z"})
        after = attention.payload(attention.build_view([], now=NOW, closed=[merged]), previous=before, now=NOW)
        self.assertEqual(after["changes"]["items"]["o/r#1"]["to_state"], "merged")
        self.assertEqual(after["changes"]["summary"], {"new": 0, "moved": 1, "gone": 0})

    def test_a_jira_reply_moves_the_card_and_wakes_an_ack(self):
        """Jira's status never shows a comment, so a ticket's comment count is the
        only witness that somebody wrote to you: a reply moves the row, labels the
        move and restamps `lastChangedAt` — the stamp an ack compares itself to,
        which is how a reply wakes one. The first generation that carries a count
        sets the baseline instead of calling every commented ticket moved."""
        def ticket(replies, with_count=True):
            row = {"key": "RBT-9", "summary": "t",
                   "status": {"name": "In Progress", "statusCategory": {"name": "In Progress"}},
                   "issueType": {"name": "Task"}}
            if with_count:
                row["comment"] = {"total": replies}
            return row

        def generation(replies, previous, at=NOW, with_count=True):
            items = attention.items_from_jira([ticket(replies, with_count)])
            return attention.payload(attention.build_view(items, now=at), previous=previous, now=at)

        def the_card(payload):
            return next(r for t in payload["tiers"] for r in t["items"] if r["ref"] == "RBT-9")

        first = generation(2, None)
        second = generation(2, first)
        self.assertEqual(second["changes"]["summary"]["moved"], 0)     # same count: nothing landed
        self.assertEqual(the_card(second)["comment_count"], 2)

        third = generation(3, second, at=NOW + timedelta(hours=1))
        self.assertEqual(third["changes"]["summary"]["moved"], 1)
        self.assertEqual(third["changes"]["items"]["RBT-9"]["label"], "a reply landed")
        moved = the_card(third)
        self.assertEqual(moved["lastChangedAt"], "2026-09-29T13:00:00Z")
        self.assertEqual((moved["section"], moved["states"][0]["key"]), ("waiting", "in-progress"))

        # a read that never asked for comments sets the baseline, never a move
        plain = attention.payload(attention.build_view(
            attention.items_from_jira([ticket(2, with_count=False)]), now=NOW), now=NOW)
        self.assertNotIn("comment_count", the_card(plain))
        arriving = generation(2, plain)                     # the count arrives: a baseline
        self.assertEqual(arriving["changes"]["summary"]["moved"], 0)
        self.assertEqual(the_card(arriving)["comment_count"], 2)
        self.assertEqual(generation(3, arriving, at=NOW + timedelta(hours=1))
                         ["changes"]["summary"]["moved"], 1)   # and from there, a reply moves it

    def test_a_card_moving_into_the_closed_log_moves_its_children_too(self):
        """The closed log holds a merged PR with the issue it closes as a child.
        That child rode out of the tiers with its parent, so the diff moves it —
        reporting it gone is what a surface then draws as a ghost in the tier it
        left, which reads as work that is still owed."""
        before = attention.payload(self.view([
            dict(self.item(1, "waiting-reply"), links=["o/r#2"]),
            dict(self.item(2, "reviewed"), links=["o/r#1"])]), now=NOW)
        after = attention.payload(attention.build_view([], now=NOW, closed=[
            dict(self.item(2, "merged"), links=["o/r#1"]),
            dict(self.item(1, "closed"), links=["o/r#2"])]), previous=before, now=NOW)
        children = after["closed"][0]["children"]
        self.assertEqual(len(children), 1)                          # it rode along
        self.assertEqual(children[0]["section"], "closed")
        self.assertEqual(after["changes"]["summary"]["gone"], 0)   # not dropped
        self.assertEqual(after["changes"]["items"][children[0]["key"]]["kind"], "moved")

    def test_a_gone_row_that_ended_closed_ghosts_into_the_closed_lane(self):
        """A member that expires out of a card it rode in belongs to the lane it
        ended in — not to the tier it was last drawn in, where 'dropped' reads as
        work still owed."""
        open_item = attention._item("ISSUE", container="o/r", ref="r#9", states=["waiting"])
        merged = attention._item("MY PR", container="o/r", ref="r#5", states=["merged"],
                                 times={"updated": "2026-09-29T09:00:00Z"}, links=["o/r#9"])
        before = attention.payload(attention.build_view([open_item], now=NOW, closed=[merged]), now=NOW)
        card = next(i for t in before["tiers"] for i in t["items"])   # one card; the PR names it
        self.assertEqual(card["ref"], "r#5")
        self.assertEqual([c["ref"] for c in card["children"]], ["r#9"])
        after = attention.payload(attention.build_view([open_item], now=NOW, closed=[]), previous=before, now=NOW)
        self.assertEqual([g["section"] for g in after["changes"]["gone"]], ["closed"])
        self.assertEqual(after["changes"]["gone"][0]["ref"], "r#5")

    def test_closed_cards_are_never_new(self):
        """A PR that closes while you are away enters Recently closed for the
        first time — that is a closure, not a new card."""
        reviewed = attention._item("REVIEWED", container="o/r", ref="o/r#9", title="pr 9",
                                   url="https://x/9", states=["merged"],
                                   times={"updated": "2026-09-28T14:00:00Z"})
        after = attention.payload(attention.build_view([], now=NOW, closed=[reviewed]), now=NOW)
        row = after["closed"][0]
        self.assertEqual(row["section"], "closed")
        self.assertNotIn("change", row)
        self.assertEqual(after["changes"]["items"], {})
        self.assertEqual(after["changes"]["summary"], {"new": 0, "moved": 0, "gone": 0})
        # the honest stamp is the closure time, so a reader can still see it is fresh
        self.assertEqual(row["firstSeenAt"], "2026-09-28T14:00:00Z")

    def test_the_work_ticket_keeps_naming_a_card_its_failing_pr_drags_to_needs(self):
        """The face is chosen by kind, not urgency: a Jira ticket names its card
        whatever the PR under it is doing. When the PR breaks, the card lands in
        Needs with the same face and the PR still riding as its child."""
        jira = "https://launchmetrics.atlassian.net/browse/RBT-1"
        ticket = attention._item("JIRA", source="jira", ref="RBT-1", title="ticket",
                                 states=["to-deploy"], times={"updated": "2026-09-29T08:00:00Z"})
        pr = attention._item("MY PR", container="o/r", ref="r#7", title="pr", jira=jira,
                             states=["waiting"], times={"updated": "2026-09-29T09:00:00Z"})
        before = attention.payload(attention.build_view([ticket, pr], now=NOW), now=NOW)
        card = before["tiers"][1]["items"][0]
        self.assertEqual(card["ref"], "RBT-1")                    # the ticket leads
        self.assertEqual([c["ref"] for c in card["children"]], ["r#7"])
        failed = dict(pr, states=["ci-failing"])
        after = attention.payload(attention.build_view([ticket, failed], now=NOW),
                                  previous=before, now=NOW)
        needs = after["tiers"][0]["items"][0]                     # the failing PR lands it
        self.assertEqual(needs["ref"], "RBT-1")
        self.assertEqual([c["ref"] for c in needs["children"]], ["r#7"])
        self.assertEqual(after["changes"]["gone"], [])

    def test_a_ticket_the_header_names_inline_is_not_reported_gone(self):
        """#24: a header names its linked Jira ticket inline rather than as a
        child, so that key is on screen even once the Jira read stops returning
        the item. Calling it dropped ghosts it back into the tier it left."""
        jira = "https://launchmetrics.atlassian.net/browse/RBT-1"
        ticket = attention._item("JIRA", source="jira", ref="RBT-1", title="ticket",
                                 states=["to-deploy"], times={"updated": "2026-09-29T08:00:00Z"})
        pr = attention._item("MY PR", container="o/r", ref="r#7", title="pr", jira=jira,
                             states=["waiting"], times={"updated": "2026-09-29T09:00:00Z"})
        before = attention.payload(attention.build_view([ticket, pr], now=NOW), now=NOW)
        later = NOW + timedelta(hours=1)
        after = attention.payload(attention.build_view([pr], now=later),
                                  previous=before, now=later)
        self.assertEqual(after["tiers"][2]["items"][0]["ref"], "r#7")
        self.assertEqual(after["changes"]["gone"], [])            # still named inline

    def test_rows_carry_their_section(self):
        view = self.view([self.item(1, "waiting")])
        snapshot = attention.payload(view, now=NOW)
        self.assertEqual(snapshot["tiers"][2]["items"][0]["section"], "waiting")


class DroppedConnections(unittest.TestCase):
    """A reader that goes away mid-request (reload during a slow refresh, tab
    closed) must not print a stack trace; a real bug still must."""

    def handle(self, exc):
        server = attention.Server.__new__(attention.Server)   # no socket bound
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                raise exc
            except type(exc):
                server.handle_error(None, None)
        return err.getvalue()

    def test_disconnects_are_silent(self):
        self.assertEqual(self.handle(BrokenPipeError(32, "Broken pipe")), "")
        self.assertEqual(self.handle(ConnectionResetError(54, "Connection reset")), "")

    def test_other_errors_still_report(self):
        self.assertIn("ValueError: real bug", self.handle(ValueError("real bug")))


if __name__ == "__main__":
    unittest.main()
