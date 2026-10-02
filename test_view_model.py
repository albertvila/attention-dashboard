#!/usr/bin/env python3
"""Fixture tests for the GitHub attention POC.

Run:  python3 -m unittest -v test_view_model

Feeds recorded real `gh` output (fixtures/gh_output.json) through the adapters
and the pure view-model seam. External behavior only: states, tiers, ordering,
bot filtering + hidden count, and error passthrough.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gh_attention_poc as poc

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "gh_output.json").read_text())
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
ME = FIXTURES["me"]


def build():
    pr_items = poc.items_from_own_prs(
        FIXTURES["search_author"], FIXTURES["pr_view"], FIXTURES["pr_checks"], FIXTURES["pr_review_threads"], ME)
    issue_items = poc.items_from_issues(
        FIXTURES["search_assignee"], FIXTURES["issue_view"], FIXTURES["issue_linked_prs"], ME)
    review_items, hidden = poc.items_from_review_search(FIXTURES["search_review_requested"])
    return poc.build_view(review_items + pr_items + issue_items, hidden_bots=hidden, now=NOW), hidden


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

    def test_review_row_carries_author_and_jira_from_branch(self):
        view, _ = build()
        row = find(view, "web-frontend#2018")
        self.assertEqual(row["author"], "lm-qinfei")
        self.assertEqual(row["jira"], "https://launchmetrics.atlassian.net/browse/BIT-9001")
        self.assertEqual(row["container"], "acme/web-frontend")
        row = find(view, "web-frontend#2017")
        self.assertEqual(row["jira"], "")

    def test_labels_are_normalized(self):
        rows = [{"number": 1, "title": "t", "url": "u", "isDraft": False,
                 "createdAt": "2026-09-29T10:00:00Z", "updatedAt": "2026-09-29T10:00:00Z",
                 "repository": {"nameWithOwner": "o/r"}, "author": {"login": "someone"},
                 "labels": [{"id": "x", "name": "bug", "description": "d", "color": "d73a4a"}]}]
        items, _ = poc.items_from_review_search(rows)
        self.assertEqual(items[0]["labels"], [{"name": "bug", "color": "d73a4a"}])


class OwnPRs(unittest.TestCase):
    def test_335_needs_comments_from_bot_thread(self):
        view, _ = build()
        row = find(view, "checkout-api#335")
        self.assertEqual([s["key"] for s in row["states"]], ["needs-comments"])
        self.assertTrue(row["detail"].startswith(
            "1 unresolved thread · review-bot: **[NIT]**"), row["detail"])
        self.assertIn("checkout-api#335", refs(view, "needs"))

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
        self.assertEqual(poc.pr_states(view, [], mine, me="me"), ["waiting-reply"])
        self.assertEqual(poc.pr_states(view, [], theirs, me="me"), ["needs-comments"])

    def test_pr_facts(self):
        view = {"reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN"}
        self.assertEqual(poc._pr_facts(view, [{"name": "ci", "state": "SUCCESS"}]),
                         [{"label": "approved", "tone": "ok"},
                          {"label": "checks green", "tone": "ok"},
                          {"label": "mergeable", "tone": "ok"}])
        view = {"reviewDecision": "REVIEW_REQUIRED", "mergeStateStatus": "BLOCKED"}
        self.assertEqual(poc._pr_facts(view, []),
                         [{"label": "awaiting review", "tone": "warn"},
                          {"label": "no checks", "tone": "warn"},
                          {"label": "merge blocked", "tone": "warn"}])

    def test_unresolved_thread_alone_is_one_state(self):
        view_335 = FIXTURES["pr_view"]["acme/checkout-api#335"]
        # all checks green, no approval, blocked: only the unresolved thread
        self.assertEqual(poc.pr_states(view_335, FIXTURES["pr_checks"]["acme/checkout-api#335"],
                                       FIXTURES["pr_review_threads"]["acme/checkout-api#335"], ME),
                         ["needs-comments"])

    def test_conflicts_state_and_tier(self):
        view = {"state": "OPEN", "isDraft": True, "mergeStateStatus": "DIRTY",
                "reviewDecision": "REVIEW_REQUIRED"}
        self.assertEqual(poc.pr_states(view, [], {"reviewThreads": {"nodes": []}}), ["conflicts"])
        threads = {"reviewThreads": {"nodes": [{"isResolved": False,
                                                   "comments": {"nodes": [{"author": {"login": "someone"}}]}}]}}
        self.assertEqual(poc.pr_states(view, [], threads), ["needs-comments", "conflicts"])
        built = poc.build_view([{"source": "github", "ref": "r#1", "states": ["conflicts"],
                                 "times": {}}], now=NOW)
        self.assertEqual(built["tiers"][0]["items"][0]["ref"], "r#1")

    def test_ci_failing(self):
        failing = [{"name": "build", "state": "FAILURE"}]
        view = {"state": "OPEN", "reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN", "isDraft": False}
        self.assertEqual(poc.pr_states(view, failing, {"reviewThreads": {"nodes": []}}), ["ci-failing"])

    def test_running_check_is_not_failing(self):
        running = [{"name": "Unit tests (docs) / Build and test", "state": "IN_PROGRESS"}]
        view = {"state": "OPEN", "reviewDecision": "REVIEW_REQUIRED", "mergeStateStatus": "BLOCKED", "isDraft": False}
        self.assertEqual(poc.pr_states(view, running, {"reviewThreads": {"nodes": []}}), ["waiting"])
        self.assertEqual(poc._pr_facts(view, running)[1], {"label": "1 checks running", "tone": "warn"})
        self.assertEqual(poc._pr_facts(view, running + [{"name": "lint", "state": "FAILURE"}])[1],
                         {"label": "1 checks failing", "tone": "bad"})


class JiraEnrichment(unittest.TestCase):
    def test_one_fetch_per_key_and_view_model_passthrough(self):
        items = [{"source": "github", "ref": "r#1", "states": ["ready"], "times": {},
                  "jira": "https://x/browse/FIRE-1"},
                 {"source": "github", "ref": "r#2", "states": ["ready"], "times": {},
                  "jira": "https://x/browse/FIRE-1"},
                 {"source": "github", "ref": "r#3", "states": ["ready"], "times": {}, "jira": ""}]
        calls = []

        def fetch(key):
            calls.append(key)
            return {"summary": "s", "status": "Open", "status_category": "To Do", "type": "Bug"}

        poc._with_jira(items, [], fetch=fetch)
        self.assertEqual(calls, ["FIRE-1"])
        self.assertEqual(items[0]["jira_issue"]["summary"], "s")
        view = poc.build_view(items, now=NOW)
        # r#1 and r#2 share a Jira key, so they cluster; r#3 stays separate.
        header = find(view, "r#1")
        self.assertEqual([c["ref"] for c in header["children"]], ["r#2"])
        self.assertEqual(header["jira_issue"]["summary"], "s")
        self.assertIsNone(find(view, "r#3")["jira_issue"])

    def test_failure_is_reported_not_fatal(self):
        items = [{"jira": "https://x/browse/FIRE-1"}]
        errors = []

        def fetch(key):
            raise poc.CliError("twg jira workitem get FIRE-1", "not found")

        poc._with_jira(items, errors, fetch=fetch)
        self.assertNotIn("jira_issue", items[0])
        self.assertEqual(errors[0]["where"], "jira FIRE-1")
        self.assertEqual(errors[0]["output"], "not found")


class JiraSource(unittest.TestCase):
    def test_jira_description_links_to_github_thread(self):
        rows = [
            {"key": "RBT-1", "summary": "s",
             "status": {"name": "Ready to Test", "statusCategory": {"name": "In Progress"}},
             "issueType": {"name": "Task"},
             "description": {"type": "doc", "content": [{"type": "paragraph", "content": [
                 {"type": "text", "text": "Spec: https://github.com/acme/checkout-api/issues/331"}]}]}},
        ]
        items = poc.items_from_jira(rows)
        self.assertEqual(items[0]["links"], ["acme/checkout-api#331"])

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
        items = poc.items_from_jira(rows)
        self.assertEqual([i["ref"] for i in items], ["RBT-1", "RBT-2", "RBT-4"])
        self.assertEqual(items[0]["states"], ["in-progress"])
        self.assertEqual(items[0]["detail"], "Task")
        self.assertEqual(items[0]["facts"], [{"label": "Ready to Test", "tone": "warn"}])
        self.assertEqual(items[0]["times"]["updated"], "2026-09-28T15:51:28.377+02:00")
        self.assertEqual(poc.humanize_age(items[0]["times"]["updated"], NOW), "22h")
        self.assertEqual(items[1]["states"], ["not-started"])
        self.assertEqual(items[1]["facts"], [{"label": "To Do", "tone": "waiting"}])
        self.assertEqual(items[1]["url"], "https://launchmetrics.atlassian.net/browse/RBT-2")
        self.assertEqual(items[2]["states"], ["to-deploy"])
        self.assertEqual(items[2]["facts"], [{"label": "TO_DEPLOY", "tone": "warn"}])
        view = poc.build_view(items, now=NOW)
        self.assertIn("RBT-1", [r["ref"] for r in view["tiers"][2]["items"]])
        self.assertIn("RBT-2", [r["ref"] for r in view["tiers"][0]["items"]])
        self.assertIn("RBT-4", [r["ref"] for r in view["tiers"][1]["items"]])


class Clustering(unittest.TestCase):
    def test_linked_pr_issue_spec_become_one_card(self):
        view, _ = build()
        header = find(view, "checkout-api#335")
        self.assertEqual(sorted(c["ref"] for c in header["children"]),
                         ["checkout-api#331", "checkout-api#332"])
        self.assertIn("checkout-api#335", refs(view, "needs"))
        self.assertNotIn("checkout-api#331", refs(view, "waiting"))
        self.assertNotIn("checkout-api#332", refs(view, "waiting"))
        self.assertEqual(find(view, "checkout-api#331")["labels"], [{"name": "spec", "color": "7057FF"}])

    def test_pr_title_issue_ref_links_the_issue(self):
        row = {"number": 2331, "title": "feat: #2330 Drop Instagram stories from monthly and hourly final files",
               "repository": {"nameWithOwner": "acme/payments-api"}, "author": {"login": "albertvila"},
               "url": "https://github.com/acme/payments-api/pull/2331", "labels": [],
               "isDraft": False, "createdAt": "2026-09-29T13:30:00Z", "updatedAt": "2026-09-29T14:00:00Z"}
        items, _ = poc.items_from_review_search([row])
        self.assertEqual(items[0]["links"], ["acme/payments-api#2330"])
        issue = {"source": "github", "chip": "ISSUE", "container": "acme/payments-api",
                 "ref": "payments-api#2330", "url": "", "states": ["waiting-reply"], "times": {}}
        view = poc.build_view(items + [issue], now=NOW)
        header = find(view, "payments-api#2331")
        self.assertEqual([c["ref"] for c in header["children"]], ["payments-api#2330"])

    def test_children_keep_facts_detail_and_labels(self):
        items = [
            {"source": "github", "chip": "MY PR", "container": "o/r", "ref": "r#1", "url": "",
             "states": ["waiting"], "times": {}, "detail": "awaiting review",
             "facts": [{"label": "checks green", "tone": "ok"}],
             "labels": [{"name": "bug", "color": "d73a4a"}]},
            {"source": "github", "chip": "ISSUE", "container": "o/r", "ref": "r#2", "url": "",
             "states": ["ready"], "times": {}, "links": ["o/r#1"]},
        ]
        view = poc.build_view(items, now=NOW)
        child = find(view, "r#1")
        self.assertEqual(child["facts"], [{"label": "checks green", "tone": "ok"}])
        self.assertEqual(child["detail"], "awaiting review")
        self.assertEqual(child["labels"], [{"name": "bug", "color": "d73a4a"}])

    def test_cluster_lands_in_its_most_urgent_tier(self):
        items = [
            {"source": "github", "chip": "ISSUE", "container": "o/r", "ref": "r#2",
             "url": "u2", "states": ["waiting"], "times": {}},
            {"source": "github", "chip": "MY PR", "container": "o/r", "ref": "r#5",
             "url": "u5", "states": ["needs-comments"], "times": {}, "links": ["o/r#2"]},
        ]
        view = poc.build_view(items, now=NOW)
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
        item = poc.items_from_issues([row], {"acme/web-frontend#2048": issue},
                                     {"acme/web-frontend#2048": empty_rel}, "me")[0]
        self.assertEqual(item["links"], ["acme/checkout-api#349"])

        pr = {"source": "github", "chip": "MY PR", "container": "acme/checkout-api",
              "ref": "checkout-api#349", "title": "feat: move data unification", "url": "u",
              "states": ["waiting"], "times": {"updated": "2026-09-29T11:00:00Z"}}
        view = poc.build_view([item, pr], now=NOW)
        cards = [i for t in view["tiers"] for i in t["items"]]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["key"], "acme/web-frontend#2048")
        self.assertEqual([c["key"] for c in cards[0]["children"]], ["acme/checkout-api#349"])

    def test_draft_cluster_goes_to_drafts_with_children(self):
        items = [
            {"source": "github", "chip": "ISSUE", "container": "o/r", "ref": "r#2",
             "url": "", "states": ["waiting"], "times": {}},
            {"source": "github", "chip": "MY PR", "container": "o/r", "ref": "r#5", "draft": True,
             "url": "", "states": ["needs-comments"], "times": {}, "links": ["o/r#2"]},
        ]
        view = poc.build_view(items, now=NOW)
        self.assertEqual([r["ref"] for r in view["drafts"]], ["r#5"])
        self.assertEqual([c["ref"] for c in view["drafts"][0]["children"]], ["r#2"])
        self.assertEqual([r["ref"] for t in view["tiers"] for r in t["items"]], [])

    def test_jira_key_and_description_join_the_cluster(self):
        items = [
            {"source": "github", "chip": "ISSUE", "container": "o/r", "ref": "r#1",
             "url": "", "states": ["waiting"], "times": {}, "links": ["o/r#2"]},
            {"source": "github", "chip": "MY PR", "container": "o/r", "ref": "r#2",
             "url": "", "states": ["waiting"], "times": {}, "jira": "https://x/browse/RBT-1"},
            {"source": "jira", "chip": "JIRA", "ref": "RBT-1", "url": "",
             "states": ["to-deploy"], "times": {}, "links": []},
        ]
        view = poc.build_view(items, now=NOW)
        self.assertEqual(refs(view, "ready"), ["RBT-1"])
        self.assertEqual(sorted(c["ref"] for c in find(view, "RBT-1")["children"]),
                         ["r#1", "r#2"])
    def test_header_jira_ticket_is_not_repeated_as_child(self):
        items = [
            {"source": "github", "chip": "MY PR", "container": "o/r", "ref": "r#5",
             "url": "", "states": ["ready"], "times": {}, "jira": "https://x/browse/ABC-1"},
            {"source": "jira", "chip": "JIRA", "ref": "ABC-1", "url": "",
             "states": ["in-progress"], "times": {}},
        ]
        view = poc.build_view(items, now=NOW)
        self.assertEqual(find(view, "r#5")["children"], [])
class ClosedLog(unittest.TestCase):
    def test_within_window(self):
        self.assertTrue(poc._within_window("2026-09-29T08:00:00Z", NOW))
        self.assertFalse(poc._within_window("2026-09-28T08:00:00Z", NOW))
        self.assertTrue(poc._within_window("2026-09-29T10:28:56.277+02:00", NOW))
        self.assertFalse(poc._within_window(None, NOW))

    def test_closed_pr_item_marks_merged_vs_closed(self):
        node = {"number": 7, "title": "cherry-pick of #3", "url": "u", "state": "MERGED",
                "closedAt": "2026-09-29T09:00:00Z", "createdAt": "2026-09-01T09:00:00Z",
                "headRefName": "m-chore-x-ABC-1",
                "repository": {"nameWithOwner": "o/r"}, "author": {"login": "me"},
                "labels": {"nodes": [{"name": "bug", "color": "d73a4a"}]},
                "closingIssuesReferences": {"nodes": [{"number": 2, "repository": {"nameWithOwner": "o/r"}}]}}
        item = poc._closed_pr_item(node, "MY PR")
        self.assertEqual(item["states"], ["merged"])
        self.assertEqual(item["links"], ["o/r#2", "o/r#3"])
        self.assertEqual(item["jira"], "https://launchmetrics.atlassian.net/browse/ABC-1")
        self.assertEqual(item["labels"], [{"name": "bug", "color": "d73a4a"}])
        node["state"] = "CLOSED"
        item = poc._closed_pr_item(node, "REVIEWED")
        self.assertEqual(item["states"], ["closed"])
        self.assertEqual(item["chip"], "REVIEWED")

    def test_closed_issue_item_links_parent_subissues_and_prs(self):
        node = {"number": 2, "title": "t", "url": "u", "closedAt": "2026-09-29T09:00:00Z",
                "createdAt": "2026-09-01T09:00:00Z", "repository": {"nameWithOwner": "o/r"},
                "body": "Jira spec: https://launchmetrics.atlassian.net/browse/RBT-7",
                "labels": {"nodes": []},
                "parent": {"number": 1, "repository": {"nameWithOwner": "o/r"}},
                "subIssues": {"nodes": [{"number": 3, "repository": {"nameWithOwner": "o/r"}}]},
                "closedByPullRequestsReferences": {"nodes": [{"number": 5, "repository": {"nameWithOwner": "o/r"}}]}}
        item = poc._closed_issue_item(node)
        self.assertEqual(sorted(item["links"]), ["o/r#1", "o/r#3", "o/r#5"])
        self.assertEqual(item["jira"], "https://launchmetrics.atlassian.net/browse/RBT-7")

    def test_closed_jira_uses_status_change_date(self):
        rows = [{"key": "ABC-1", "summary": "s", "url": "u",
                 "status": {"name": "Done"}, "issueType": {"name": "Task"},
                 "description": {"type": "doc", "content": [{"type": "paragraph", "content": [
                     {"type": "text", "text": "Spec: https://github.com/o/r/issues/2"}]}]},
                 "statuscategorychangeddate": "2026-09-29T10:28:56.277+0200",
                 "updated": "2026-09-29T12:00:22.383+0200"}]
        item = poc.items_from_jira_closed(rows)[0]
        self.assertEqual(item["states"], ["done"])
        self.assertEqual(item["times"]["updated"], "2026-09-29T10:28:56.277+02:00")
        self.assertEqual(item["detail"], "Done · Task")
        self.assertEqual(item["links"], ["o/r#2"])

    def test_closed_items_cluster_with_their_open_links(self):
        """A merged PR and the open item it links to stay one card; the merged
        state ranks above waiting, so the card moves to Ready."""
        closed = [
            {"source": "github", "chip": "ISSUE", "container": "o/r", "ref": "r#2", "url": "",
             "states": ["closed"], "times": {"updated": "2026-09-29T09:01:00Z"}, "links": ["o/r#5"]},
            {"source": "github", "chip": "MY PR", "container": "o/r", "ref": "r#5", "url": "",
             "states": ["merged"], "times": {"updated": "2026-09-29T09:00:00Z"}, "links": ["o/r#2"]},
        ]
        open_items = [{"source": "github", "chip": "ISSUE", "container": "o/r", "ref": "r#9", "url": "",
                       "states": ["waiting"], "times": {}, "links": ["o/r#5"]}]
        view = poc.build_view(open_items, now=NOW, closed=closed)
        self.assertEqual(view["closed"], [])
        cards = {t["key"]: t["items"] for t in view["tiers"]}
        self.assertEqual([r["ref"] for r in cards["ready"]], ["r#5"])
        self.assertEqual([c["ref"] for c in cards["ready"][0]["children"]], ["r#9", "r#2"])

    def test_section_only_move_does_not_repeat_the_tier(self):
        """A card coming back out of the closed log has the same state and tier;
        the label must not read 'Waiting on others → Waiting on others'."""
        merged = {"source": "github", "chip": "MY PR", "container": "o/r", "ref": "r#7",
                  "url": "", "states": ["merged"], "times": {"updated": "2026-09-29T09:00:00Z"}}
        before = poc.payload(poc.build_view([], now=NOW, closed=[merged]), now=NOW)
        after = poc.payload(poc.build_view([dict(merged, links=["o/r#7"])], now=NOW), previous=before)
        self.assertEqual(after["changes"]["items"]["o/r#7"]["label"], "moved")

    def test_all_closed_cluster_stays_in_the_closed_log(self):
        closed = [
            {"source": "github", "chip": "ISSUE", "container": "o/r", "ref": "r#2", "url": "",
             "states": ["closed"], "times": {"updated": "2026-09-29T09:01:00Z"}, "links": ["o/r#5"]},
            {"source": "github", "chip": "MY PR", "container": "o/r", "ref": "r#5", "url": "",
             "states": ["merged"], "times": {"updated": "2026-09-29T09:00:00Z"}, "links": ["o/r#2"]},
        ]
        view = poc.build_view([], now=NOW, closed=closed)
        self.assertEqual([t["items"] for t in view["tiers"]], [[], [], []])
        self.assertEqual(len(view["closed"]), 1)
        self.assertEqual(view["closed"][0]["ref"], "r#5")
        self.assertEqual([c["ref"] for c in view["closed"][0]["children"]], ["r#2"])

    def test_merged_pr_lifts_its_ticket_to_ready(self):
        """The ticket a merged PR names is shown inline on the card, and the
        merged PR is the header: the pair does not fall apart into Waiting."""
        ticket = {"source": "jira", "chip": "JIRA", "ref": "FIRE-1", "url": "",
                  "states": ["in-progress"], "times": {"updated": "2026-09-29T09:00:00Z"}}
        merged = {"source": "github", "chip": "MY PR", "container": "o/r", "ref": "r#7",
                  "url": "", "states": ["merged"],
                  "times": {"updated": "2026-09-29T09:30:00Z"},
                  "jira": poc.JIRA_BASE + "FIRE-1"}
        view = poc.build_view([ticket], now=NOW, closed=[merged])
        cards = [i for t in view["tiers"] for i in t["items"]]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["tier"], "ready")
        self.assertEqual(cards[0]["ref"], "r#7")
        self.assertEqual(cards[0]["children"], [])   # the ticket is inline, not a child
        self.assertEqual(cards[0]["jira"], poc.JIRA_BASE + "FIRE-1")
        self.assertEqual(view["closed"], [])

    def test_closed_bucket_is_newest_first_and_not_in_tiers(self):
        open_items = [{"source": "github", "chip": "MY PR", "container": "o/r", "ref": "r#1",
                       "url": "", "states": ["ready"], "times": {}}]
        closed = [{"source": "github", "chip": "MY PR", "container": "o/r", "ref": "r#2", "url": "",
                   "states": ["merged"], "times": {"updated": "2026-09-29T11:00:00Z"}},
                  {"source": "github", "chip": "ISSUE", "container": "o/r", "ref": "r#3", "url": "",
                   "states": ["closed"], "times": {"updated": "2026-09-29T05:00:00Z"}}]
        view = poc.build_view(open_items, now=NOW, closed=closed)
        self.assertEqual([r["ref"] for r in view["closed"]], ["r#2", "r#3"])
        self.assertEqual([r["ref"] for t in view["tiers"] for r in t["items"]], ["r#1"])
        self.assertEqual([s["key"] for s in view["closed"][0]["states"]], ["merged"])
        self.assertEqual(view["closed"][0]["age"], "1h")


class CheckParsing(unittest.TestCase):
    def test_no_checks_reported_is_empty_not_error(self):
        def gh(args):
            raise poc.CliError("gh pr checks", "no checks reported on the 'fix/x' branch")

        self.assertEqual(poc._pr_checks(gh, "o/r", 1), [])

    def test_other_check_errors_still_raise(self):
        def gh(args):
            raise poc.CliError("gh pr checks", "HTTP 500")

        with self.assertRaises(poc.CliError):
            poc._pr_checks(gh, "o/r", 1)


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
        items = poc.items_from_issues(FIXTURES["search_assignee"], FIXTURES["issue_view"],
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
        item = poc._issue_item(view, ["not-started"], [], {})
        self.assertEqual(item["jira"], "https://launchmetrics.atlassian.net/browse/RBT-9003")
        jira = {"source": "jira", "chip": "JIRA", "ref": "RBT-9003", "url": "",
                "states": ["waiting"], "times": {}, "links": []}
        built = poc.build_view([item, jira], now=NOW)
        # one card, headed by the issue; the Jira ticket is shown inline, not as child.
        self.assertEqual([r["ref"] for r in built["tiers"][0]["items"]], ["payments-api#2330"])
        self.assertEqual(built["tiers"][0]["items"][0]["children"], [])

    def test_needs_reply_and_not_started(self):
        states, prs = poc.issue_states({"comments": [{"author": {"login": "someone"}}]}, {}, ME)
        self.assertEqual(states, ["needs-reply"])
        states, prs = poc.issue_states({"comments": []}, {}, ME)
        self.assertEqual(states, ["not-started"])
        view = poc.build_view([{"source": "github", "ref": "r#1", "states": states, "times": {}}], now=NOW)
        self.assertEqual(view["tiers"][0]["items"][0]["ref"], "r#1")


class OrderingAndErrors(unittest.TestCase):
    def test_stalest_member_is_header_across_time_offsets(self):
        items = [
            {"source": "github", "chip": "ISSUE", "container": "o/r", "ref": "r#2330",
             "url": "", "states": ["waiting-reply"], "times": {"updated": "2026-09-29T12:58:59Z"}},
            {"source": "jira", "chip": "JIRA", "ref": "RBT-9003", "url": "", "states": ["in-progress"],
             "times": {"updated": "2026-09-29T14:00:47.547+02:00"}, "links": ["o/r#2330"]},
        ]
        view = poc.build_view(items, now=NOW)
        self.assertEqual([r["ref"] for r in view["tiers"][2]["items"]], ["RBT-9003"])
        self.assertEqual([c["ref"] for c in find(view, "RBT-9003")["children"]], ["r#2330"])

    def test_tier_order_is_chronological_across_time_offsets(self):
        items = [
            {"ref": "newer", "states": ["waiting"], "times": {"updated": "2026-09-29T12:58:59Z"}},
            {"ref": "older", "states": ["waiting"], "times": {"updated": "2026-09-29T14:00:47.547+02:00"}},
        ]
        view = poc.build_view(items, now=NOW)
        self.assertEqual([r["ref"] for r in view["tiers"][2]["items"]], ["older", "newer"])

    def test_stalest_activity_first_within_tier(self):
        items = [
            {"ref": "a", "states": ["review-requested"], "times": {"updated": "2026-09-29T10:00:00Z"}},
            {"ref": "b", "states": ["review-requested"], "times": {"updated": "2026-09-20T10:00:00Z"}},
            {"ref": "c", "states": ["review-requested"], "times": {"updated": "2026-09-25T10:00:00Z"}},
        ]
        view = poc.build_view(items, now=NOW)
        self.assertEqual([r["ref"] for r in view["tiers"][0]["items"]], ["b", "c", "a"])

    def test_errors_pass_through_never_empty_silently(self):
        err = {"where": "review requests", "command": "gh search prs", "output": "boom"}
        view = poc.build_view([], errors=[err], now=NOW)
        self.assertEqual(view["errors"], [err])

    def test_issue_refs_in_skips_cross_repo_refs(self):
        self.assertEqual(poc._issue_refs_in("feat: #2330 Drop stories", "o/r"), ["o/r#2330"])
        self.assertEqual(poc._issue_refs_in("see acme/shared-lib#409", "o/r"), [])
        self.assertEqual(poc._issue_refs_in(None, "o/r"), [])

    def test_jira_url_in_only_matches_atlassian_browse_links(self):
        self.assertEqual(poc._jira_url_in("Jira spec: https://launchmetrics.atlassian.net/browse/RBT-9003 (UTF-8 ok)"),
                         "https://launchmetrics.atlassian.net/browse/RBT-9003")
        self.assertEqual(poc._jira_url_in("mentions UTF-8 and RBT-9003 as plain text"), "")
        self.assertEqual(poc._jira_url_in("https://example.com/browse/RBT-9003"), "")
        self.assertEqual(poc._jira_url_in(None), "")

    def test_jira_url_only_matches_uppercase_key(self):
        self.assertEqual(poc.jira_url("m-chore-bump_lm_data_unification_3_9_6-FIRE-9001"),
                         "https://launchmetrics.atlassian.net/browse/FIRE-9001")
        self.assertEqual(poc.jira_url("fix-123-something"), "")
        self.assertEqual(poc.jira_url("bb/orchestrate-bb-plan-https-github-com-launchmetri"), "")
        self.assertEqual(poc.jira_url(""), "")
        self.assertEqual(poc.jira_url(None), "")

    def test_humanize_age(self):
        self.assertEqual(poc.humanize_age("2026-09-29T11:30:00Z", NOW), "30m")
        self.assertEqual(poc.humanize_age("2026-09-29T05:00:00Z", NOW), "7h")
        self.assertEqual(poc.humanize_age("2026-09-24T12:00:00Z", NOW), "5d")
        self.assertEqual(poc.humanize_age("2026-07-24T12:00:00Z", NOW), "2mo")


class Snoozes(unittest.TestCase):
    """Snooze is user intent in its own file: nothing about the snapshot moves."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="snooze-test-")
        self.path = os.path.join(self.dir, "snoozes.json")

    def test_add_expiry_and_prune_on_write(self):
        soon = poc._iso_utc(NOW + timedelta(hours=1))
        self.assertEqual(poc.write_snooze("o/r#1", soon, path=self.path, now=NOW), {"o/r#1": soon})
        self.assertEqual(poc.load_snoozes(self.path, now=NOW), {"o/r#1": soon})
        # an expired key disappears from the read, and from the file on the next write
        later = NOW + timedelta(hours=2)
        self.assertEqual(poc.load_snoozes(self.path, now=later), {})
        later_still = poc._iso_utc(NOW + timedelta(hours=4))
        self.assertEqual(poc.write_snooze("o/r#2", later_still, path=self.path, now=later),
                         {"o/r#2": later_still})

    def test_wake_removes_the_key(self):
        until = poc._iso_utc(NOW + timedelta(hours=1))
        poc.write_snooze("o/r#1", until, path=self.path, now=NOW)
        poc.write_snooze("o/r#2", until, path=self.path, now=NOW)
        self.assertEqual(poc.write_snooze("o/r#1", "", path=self.path, now=NOW), {"o/r#2": until})

    def test_missing_or_broken_file_is_just_empty(self):
        self.assertEqual(poc.load_snoozes(self.path), {})
        with open(self.path, "w") as fh:
            fh.write("not json")
        self.assertEqual(poc.load_snoozes(self.path), {})

    def test_snooze_request_hours_and_wake(self):
        path = poc.snooze_path
        poc.snooze_path = lambda: self.path
        try:
            data = poc.snooze({"key": "o/r#1", "hours": 4})
            self.assertEqual(list(data), ["o/r#1"])
            self.assertEqual(poc.snooze({"key": "o/r#1", "hours": 0}), {})
            self.assertEqual(poc.snooze({}), {"error": "key is required"})
        finally:
            poc.snooze_path = path


class MailSource(unittest.TestCase):
    """gmcli threads -> cards; stars are the gate, group/ labels merge."""

    THREADS = [{"id": "t1", "messages": [
        {"id": "m1", "threadId": "t1", "labelIds": ["INBOX", "STARRED", "Label_g1"],
         "snippet": "first &amp; older", "internalDate": "1790764004000",
         "from": '"Antoni Parramon Naranjo" <antoni@example.com>',
         "subject": "[JIRA] Antoni mentioned you on FIRE-72208", "hasAttachments": False},
        {"id": "m2", "threadId": "t1", "labelIds": ["INBOX", "STARRED", "UNREAD", "Label_g1"],
         "snippet": "second &amp; newer", "internalDate": "1790775262000",
         "from": '"Antoni Parramon Naranjo" <antoni@example.com>',
         "subject": "Re: [JIRA] Antoni mentioned you on FIRE-72208", "hasAttachments": True},
    ]}]

    def test_thread_becomes_a_card(self):
        item = poc.items_from_mail(self.THREADS, {"Label_g1": "group/summit"})[0]
        self.assertEqual(item["key"], "mail/t1")
        self.assertEqual(item["chip"], "MAIL")
        self.assertEqual(item["states"], ["needs-reply"])
        self.assertEqual(item["author"], "Antoni Parramon Naranjo")
        self.assertEqual(item["jira"], poc.JIRA_BASE + "FIRE-72208")
        self.assertEqual(item["links"], ["group/summit"])
        self.assertEqual([f["label"] for f in item["facts"]],
                         ["unread", "2 messages", "attachment", "group/summit"])
        self.assertEqual(item["detail"], "second & newer")
        self.assertEqual(item["times"], {"updated": "2026-09-30T13:34:22Z",
                                          "created": "2026-09-30T10:26:44Z"})
        self.assertIn("authuser=you%40launchmetrics.com", item["url"])
        self.assertTrue(item["url"].endswith("#all/t1"))

    def test_no_group_label_means_no_link(self):
        self.assertEqual(poc.items_from_mail(self.THREADS)[0]["links"], [])

    def test_group_labels_are_read_from_the_table(self):
        table = ("ID\tNAME\tTYPE\n"
                 "Label_g1\tgroup/summit\tuser\n"
                 "Label_x\tZoom\tuser\n"
                 "INBOX\tINBOX\tsystem\n")
        errors = []
        groups = poc._mail_groups(errors, run=lambda cmd, args: table)
        self.assertEqual(groups, {"Label_g1": "group/summit"})
        self.assertEqual(errors, [])

    def test_unresolvable_jira_key_is_not_a_link(self):
        """Bare KEY-123 in free text matches UTF-8 / SHA-256 too."""
        items = poc.items_from_mail([{"id": "t9", "messages": [
            {"id": "m9", "threadId": "t9", "labelIds": ["INBOX", "STARRED"],
             "snippet": "the payload is UTF-8 encoded", "internalDate": "1790775262000",
             "from": "a@b.com", "subject": "notes about UTF-8 and SHA-256", "hasAttachments": False}]}])
        self.assertTrue(items[0]["jira"].endswith("UTF-8"))

        def missing(key):
            raise RuntimeError(f"no such ticket {key}")

        errors = []
        poc._with_jira(items, errors, fetch=missing)
        self.assertEqual(items[0]["jira"], "")
        self.assertEqual(errors, [])          # a false positive is not a source error

    def test_group_label_failure_only_costs_grouping(self):
        errors = []

        def boom(cmd, args):
            raise RuntimeError("gmcli missing")

        self.assertEqual(poc._mail_groups(errors, run=boom), {})
        self.assertEqual(len(errors), 1)
        self.assertIn("mail groups", errors[0]["where"])

    def test_threads_sharing_a_group_merge_into_one_card(self):
        second = {"id": "t2", "messages": [dict(self.THREADS[0]["messages"][0],
                                                   id="m3", threadId="t2",
                                                   internalDate="1790780000000")]}
        items = poc.items_from_mail(self.THREADS + [second], {"Label_g1": "group/summit"})
        view = poc.build_view(items, now=NOW)
        cards = [i for t in view["tiers"] for i in t["items"]]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["tier"], "needs")
        self.assertEqual([c["key"] for c in cards[0]["children"]], ["mail/t2"])
        # a mail child has no ref, so its title is what the link renders as
        self.assertEqual(cards[0]["children"][0]["title"],
                         "[JIRA] Antoni mentioned you on FIRE-72208")


class SnapshotContract(unittest.TestCase):
    """The shared JSON: stable keys, raw times, and the change block."""

    def item(self, num, state, title=None):
        return {"source": "github", "container": "o/r", "ref": f"o/r#{num}",
                "title": title or f"pr {num}", "url": f"https://x/{num}",
                "states": [state], "times": {"updated": "2026-09-28T12:00:00Z"}}

    def view(self, items):
        return poc.build_view(items, now=NOW)

    def test_rows_carry_key_times_and_tone(self):
        view = self.view([dict(self.item(1, "needs-comments"), links=["o/r#2"])])
        row = find(view, "o/r#1")
        self.assertEqual(row["key"], "o/r#1")
        self.assertEqual(row["links"], ["o/r#2"])       # why this card clustered
        self.assertEqual(row["times"], {"updated": "2026-09-28T12:00:00Z"})
        self.assertEqual(row["states"][0]["tone"], "warn")

    def test_payload_is_one_json_document(self):
        snapshot = poc.payload(self.view([self.item(1, "ready")]), now=NOW)
        self.assertEqual(snapshot["schema"], poc.SCHEMA_VERSION)
        self.assertEqual(snapshot["generatedAt"], "2026-09-29T12:00:00Z")
        self.assertIsNone(snapshot["changes"]["previousAt"])
        self.assertEqual(json.loads(json.dumps(snapshot))["tiers"], snapshot["tiers"])

    def test_changes_new_moved_and_gone(self):
        before = poc.payload(self.view([
            self.item(1, "waiting"), self.item(2, "ready"), self.item(3, "waiting")]), now=NOW)
        after = poc.payload(self.view([
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
            return {"source": "github", "container": "o/r", "ref": "o/r#1",
                    "states": states, "times": {"updated": "2026-09-28T12:00:00Z"}}
        before = poc.payload(self.view([it(["waiting-reply"])]), now=NOW)
        after = poc.payload(self.view([it(["waiting-reply", "conflicts"])]), previous=before, now=NOW)
        self.assertEqual(after["changes"]["items"]["o/r#1"]["label"],
                         "Waiting on others → Needs you now")

    def test_first_seen_and_last_change_survive_generations(self):
        first = poc.payload(self.view([self.item(1, "ready"), self.item(2, "waiting")]), now=NOW)
        later = NOW + timedelta(hours=2)
        second = poc.payload(self.view([self.item(1, "ci-failing"), self.item(2, "waiting"),
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
        third = poc.payload(self.view([self.item(1, "ci-failing"), self.item(2, "waiting"),
                                       self.item(3, "ready")]), previous=second,
                            now=NOW + timedelta(hours=4))
        moved = {r["key"]: r for t in third["tiers"] for r in t["items"]}["o/r#1"]
        self.assertNotIn("change", moved)
        self.assertEqual(moved["lastChange"]["label"], "ready → CI failing")
        self.assertEqual(moved["lastChangedAt"], "2026-09-29T14:00:00Z")

    def test_gone_rows_persist_for_a_later_look(self):
        first = poc.payload(self.view([self.item(1, "waiting"), self.item(2, "waiting")]), now=NOW)
        second = poc.payload(self.view([self.item(1, "waiting")]), previous=first, now=NOW)
        third = poc.payload(self.view([self.item(1, "waiting")]), previous=second,
                            now=NOW + timedelta(hours=1))
        self.assertEqual(second["changes"]["summary"]["gone"], 1)
        self.assertEqual(third["changes"]["summary"]["gone"], 0)
        self.assertEqual([(r["key"], r["goneAt"]) for r in third["changes"]["gone"]],
                         [("o/r#2", "2026-09-29T12:00:00Z")])

    def test_change_annotation_survives_leaving_the_tiers(self):
        before = poc.payload(self.view([self.item(1, "needs-comments")]), now=NOW)
        merged = {"source": "github", "chip": "MY PR", "container": "o/r", "ref": "o/r#1",
                  "title": "pr 1", "url": "https://x/1", "states": ["merged"],
                  "times": {"updated": "2026-09-28T12:00:00Z"}}
        after = poc.payload(poc.build_view([], now=NOW, closed=[merged]), previous=before, now=NOW)
        self.assertEqual(after["changes"]["items"]["o/r#1"]["to_state"], "merged")
        self.assertEqual(after["changes"]["summary"], {"new": 0, "moved": 1, "gone": 0})

    def test_closed_cards_are_never_new(self):
        """A PR that closes while you are away enters Recently closed for the
        first time — that is a closure, not a new card."""
        reviewed = {"source": "github", "chip": "REVIEWED", "container": "o/r", "ref": "o/r#9",
                    "title": "pr 9", "url": "https://x/9", "states": ["merged"],
                    "times": {"updated": "2026-09-28T11:00:00Z"}}
        after = poc.payload(poc.build_view([], now=NOW, closed=[reviewed]), now=NOW)
        row = after["closed"][0]
        self.assertEqual(row["section"], "closed")
        self.assertNotIn("change", row)
        self.assertEqual(after["changes"]["items"], {})
        self.assertEqual(after["changes"]["summary"], {"new": 0, "moved": 0, "gone": 0})
        # the honest stamp is the closure time, so a reader can still see it is fresh
        self.assertEqual(row["firstSeenAt"], "2026-09-28T11:00:00Z")

    def test_rows_carry_their_section(self):
        view = self.view([self.item(1, "waiting")])
        snapshot = poc.payload(view, now=NOW)
        self.assertEqual(snapshot["tiers"][2]["items"][0]["section"], "waiting")


if __name__ == "__main__":
    unittest.main()
