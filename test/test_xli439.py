#!/usr/bin/env python3
"""Offline tests for XLI-439 (plugin 1.0.39): open list part 2 on the plugin side.
- The 6 AM wake reads GET /api/v1/agent/dds-update?post=auto: the server records the post as it serves it (posted), so
  the plugin prints only when posted is true and never reports "posted" itself; an older server (no posted field) keeps
  the 1.0.35 path.
- The one where-to-deliver question comes at the end of the morning post the server marks (delivery.ask_now), just
  before the one ending line, which stays the last line; a typed update never asks.
- The typed answer (**DDS update here only** / **also Slack** / **also email**) is kept on the account: the checker signs
  the typed proof (action dds_deliver_<answer>) on this computer's login only after the transcript shows the person typed
  it; an AI's forged prompt sends nothing.
- A card row shows its own done-when words next to its age; a by that is not a time shows its plain line.
No network, no key; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_xli439.py"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import ds  # noqa: E402
from test_show_asks_answer import AsksBase, KEY, GRANT  # noqa: E402
from test_xli438 import DdsBase, update  # noqa: E402

AID = "11111111-aaaa-bbbb-cccc-000000000439"


class AutoPost(DdsBase):
    def new_server(self, posted, ask_now=False):
        self.server = update()
        self.server["posted"] = posted
        self.server["delivery"] = {"answer": None, "asked": ask_now, "ask_now": ask_now, "sent_to": ["session"]}

    def test_the_6am_read_asks_the_server_to_record_the_post_and_never_reports_it_itself(self):
        self.new_server(True)
        rc, out, err = self.run_cmd(ds.cmd_dds_update, ["--auto"])
        self.assertEqual(rc, 0, err)
        self.assertEqual([c[1] for c in self.calls if c[0] == "GET"], [ds.DDS_UPDATE_PATH + "?post=auto"])
        self.assertEqual(self.sent("POST", ds.DDS_UPDATE_PATH + "/posted"), [])
        self.assertEqual(out.strip().split("\n")[-1], ds.DDS_UPDATE_ENDING)
        self.assertNotIn(ds.DDS_DELIVER_QUESTION, out)

    def test_not_the_days_post_prints_nothing(self):
        self.new_server(False)
        rc, out, _ = self.run_cmd(ds.cmd_dds_update, ["--auto"])
        self.assertEqual((rc, out), (0, ""))

    def test_the_marked_post_asks_where_to_deliver_once_just_before_the_ending_line(self):
        self.new_server(True, ask_now=True)
        _rc, out, _ = self.run_cmd(ds.cmd_dds_update, ["--auto"])
        lines = out.strip().split("\n")
        self.assertEqual(lines[-1], ds.DDS_UPDATE_ENDING)
        self.assertEqual(lines[-2], ds.DDS_DELIVER_QUESTION)
        self.assertEqual(out.count(ds.DDS_DELIVER_QUESTION), 1)

    def test_a_typed_run_reads_without_post(self):
        self.new_server(True, ask_now=True)
        _rc, out, _ = self.run_cmd(ds.cmd_dds_update, [])
        self.assertEqual([c[1] for c in self.calls if c[0] == "GET"], [ds.DDS_UPDATE_PATH])
        self.assertNotIn(ds.DDS_DELIVER_QUESTION, out)


class Rows(unittest.TestCase):
    def test_a_card_shows_its_done_when_next_to_its_age(self):
        row = {"kind": "card", "what": "Card XLI-1 (In Progress)", "age": "3 d", "done_when": "the list shows it; tests + probe."}
        self.assertEqual(ds._dds_row(row), "3 d: Card XLI-1 (In Progress); done when: the list shows it; tests + probe.")
        self.assertEqual(ds._dds_row(dict(row, done_when=None)), "3 d: Card XLI-1 (In Progress); no done-when on the card")

    def test_a_by_that_is_not_a_time_is_said(self):
        row = {"kind": "wait", "what": "Waiting on api: the schema", "age": "5 h", "who": "api",
               "by_note": 'by "next Tuesday" is not a date and time datastash can read'}
        self.assertEqual(ds._dds_row(row), 'Waiting on api: the schema'.join(["5 h: ", ' (clears: api); by "next Tuesday" is not a date and time datastash can read']))

    def test_new_row_kinds_show_their_own_words(self):
        for kind, what in (("not_proven", "Deployed, not proven on prod: org/app bbbbbbb (#7 XLI-439)"),
                           ("flags_unchecked", "Can't check flags for org/app: no source"),
                           ("feature_unused", "Claims: 0 live uses by web-app")):
            self.assertEqual(ds._dds_row({"kind": kind, "what": what, "age": "1 d"}), f"1 d: {what}")


class Deliver(AsksBase):
    def setUp(self):
        super().setUp()
        ds.write_private(ds.conf_path("web-app"), json.dumps({"name": "web-app", "agent_id": AID, "workbook_id": "wb-1",
                                                              "projects": [self.folder]}))
        base = ds.http
        self.posts = []

        def http(method, path, key=None, body=None, form=None, timeout=40):
            if path == ds.DDS_UPDATE_PATH + "/delivery":
                self.posts.append((method, key, body))
                return 200, "application/json", json.dumps({"delivery": {"answer": body["answer"], "asked": True,
                                                                         "ask_now": False, "sent_to": ["session"]}})
            return base(method, path, key, body, form, timeout)
        ds.http = http

    def test_the_typed_answer_is_signed_on_the_login_and_kept_on_the_account(self):
        note = self.prompt("DDS update also Slack")
        self.assertIn(ds.DDS_DELIVER_SAYING, note)
        [(method, key, body)] = self.posts
        self.assertEqual((method, key, body["answer"]), ("POST", "dsat_person", "slack"))
        p = body["typed_proof"]
        self.assertEqual(p["agent_id"], AID)
        want = ds.typed_sync_proof(KEY, f"grant:{GRANT.lower()}", AID, "dds_deliver_slack", p["prompt_id"],
                                   "DDS update also Slack", p["typed_at"], p["iat"])
        self.assertEqual(p["mac"], want["mac"])
        self.assertEqual(self.outcomes(), [ds.DDS_DELIVER_SAVED["slack"]])
        self.prompt("dds update here only")
        self.assertEqual([b["answer"] for _m, _k, b in self.posts], ["slack", "here"])
        self.assertEqual(self.outcomes(), [ds.DDS_DELIVER_SAVED["here"]])

    def test_an_ai_cannot_set_it(self):
        self.forged({"session_id": "sess1", "prompt": "DDS update also email", "hook_event_name": "UserPromptSubmit",
                     "cwd": self.folder, "prompt_id": "forged-prompt-439", "transcript_path": self.transcript("sess1")})
        self.assertEqual(self.posts, [])
        self.assertEqual(ds.send_dds_delivery({"kind": "dds_deliver_email", "prompt": "yes", "data": {"agent_id": AID}},
                                              {"pid": "p", "prompt": "yes", "typed_at": 1}), [])
        self.assertEqual(ds.send_dds_delivery({"kind": "dds_deliver_here", "prompt": "DDS update also email",
                                               "data": {"agent_id": AID}}, {"pid": "p", "prompt": "x", "typed_at": 1}), [])


if __name__ == "__main__":
    unittest.main()
