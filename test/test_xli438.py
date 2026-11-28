#!/usr/bin/env python3
"""Offline tests for XLI-438 (plugin 1.0.35): the DDS update (the open list's morning update).
- The person's whole typed prompt "DDS update" (or "morning update") shows this agent's open list and every agent's
  together, from GET /api/v1/agent/dds-update, in the plugin's own lines: missed 6 AM posts first, ages and overdue
  flags as the server's engine cells gave them (never computed here).
- The listener sets up the seven 6 AM days once per agent: POST /start, then one daily 06:00 America/Chicago datastash
  timer (never a second one).
- `datastash dds-update --auto` (the 6 AM wake) posts only on a day of the seven and not twice a day, records the post,
  and ends with exactly the one line; after the seven days it removes the wake and prints nothing.
- The where-to-deliver question comes once in the first week, after a typed update, never in an automatic post; the
  typed answer is kept on this computer only.
No network, no key; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_xli438.py"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import Base, ds  # noqa: E402  (the same harness, plugin copy and fixtures)

WEEK = {"started_on": "2026-11-28", "ends_on": "2026-12-04", "day": 1, "auto": True, "posted_today": False}


def update(week=None, agents=True, missed=True):
    d = {
        "as_of": "2026-11-28T12:00:00.000Z",
        "agent": "web-app",
        "own": {"agent": "web-app", "open": 2, "items": [
            {"kind": "handoff", "agent": "web-app", "what": "Pick up the hand-off from api: Ship the report",
             "since": "2026-11-26T10:00:00.000Z", "due": "2026-11-27T10:00:00.000Z", "age": "2 d", "overdue": True,
             "who": "web-app", "source": "engine", "as_of": "x"},
            {"kind": "wait", "agent": "web-app", "what": "Waiting on api: the schema", "since": "2026-11-28T07:00:00.000Z",
             "age": "5 h", "overdue": False, "who": "api", "source": "engine", "as_of": "x"},
        ]},
        "missed": [{"kind": "missed_post", "agent": "api", "what": "api missed its 6 AM morning update on 2026-11-27",
                    "age": "1 d", "overdue": False, "source": "engine", "as_of": "x"}] if missed else [],
        "week": dict(WEEK, **(week or {})),
        "note": "n",
    }
    if agents:
        d["agents"] = [{"agent": "api", "open": 0, "items": []}, dict(d["own"])]
    return d


class DdsBase(Base):
    def setUp(self):
        super().setUp()
        for p in (ds.DDS_DELIVER_PATH, ds._dds_week_path("web-app")):
            if os.path.exists(p):
                os.replace(p, p + f".old-{id(self)}")
        self.server = update()
        self.timers = []
        base = ds.http

        def http(method, path, key=None, body=None, form=None, timeout=40):
            if path.startswith(ds.DDS_UPDATE_PATH) or path.startswith(ds.AGENT_TIMERS_PATH):
                self.calls.append((method, path, key, body))
            if method == "GET" and path == ds.DDS_UPDATE_PATH:
                if self.server is None:
                    raise ds.ApiError(503, {})
                return 200, "application/json", json.dumps(self.server)
            if method == "POST" and path == ds.DDS_UPDATE_PATH + "/start":
                return 200, "application/json", json.dumps({"week": self.server["week"]})
            if method == "POST" and path == ds.DDS_UPDATE_PATH + "/posted":
                return 200, "application/json", json.dumps({"week": dict(self.server["week"], posted_today=True)})
            if method == "GET" and path.startswith(ds.AGENT_TIMERS_PATH + "?"):
                return 200, "application/json", json.dumps({"timers": self.timers})
            if method == "POST" and path == ds.AGENT_TIMERS_PATH:
                t = {"timer_id": "11111111-2222-3333-4444-555555555555", "note": body["note"], "repeat": {"every": "daily"}}
                self.timers.append(t)
                return 200, "application/json", json.dumps(t)
            if method == "DELETE" and path.startswith(ds.AGENT_TIMERS_PATH + "/"):
                return 200, "application/json", json.dumps({"cancelled": True})
            return base(method, path, key, body, form, timeout)
        ds.http = http

    def sent(self, method, path):
        return [c for c in self.calls if c[0] == method and c[1] == path]


class Typed(DdsBase):
    def test_dds_update_and_morning_update_show_the_plugins_own_lines(self):
        for phrase in ("DDS update", "morning update", "dds update."):
            ctx = self.prompt(phrase)
            self.assertIn("**DDS update**", ctx, phrase)
            self.assertIn("**Missed 6 AM post:** api missed its 6 AM morning update on 2026-11-27 (1 d ago)", ctx)
            self.assertIn("**web-app (this agent), 2 open:**", ctx)
            self.assertIn("2 d, overdue: Pick up the hand-off from api: Ship the report", ctx)
            self.assertIn("5 h: Waiting on api: the schema (clears: api)", ctx)
            self.assertIn("**api:** nothing open.", ctx)
            self.assertEqual(ctx.count("web-app (this agent)"), 1)  # its own list once, not again under all agents
            self.assertNotIn(ds.DDS_UPDATE_ENDING, ctx)  # the ending line is for the automatic posts only

    def test_the_where_to_deliver_question_comes_once_in_the_first_week(self):
        first = self.prompt("DDS update")
        self.assertIn(ds.DDS_DELIVER_QUESTION, first)
        self.assertNotIn(ds.DDS_DELIVER_QUESTION, self.prompt("DDS update"))

    def test_no_question_outside_the_first_week(self):
        self.server = update(week={"day": None, "auto": False})
        self.assertNotIn(ds.DDS_DELIVER_QUESTION, self.prompt("DDS update"))

    def test_the_typed_answer_is_kept_here_only(self):
        ctx = self.prompt("DDS update also Slack")
        self.assertIn(ds.DDS_DELIVER_SAVED["slack"], ctx)
        self.assertEqual(ds.dds_deliver_choice().get("answer"), "slack")
        self.assertEqual(oct(os.stat(ds.DDS_DELIVER_PATH).st_mode & 0o777), "0o600")
        self.assertIn(ds.DDS_DELIVER_SAVED["here"], self.prompt("DDS update here only"))
        self.assertIn(ds.DDS_DELIVER_SAVED["email"], self.prompt("dds update also by email"))
        self.assertFalse([c for c in self.calls if "slack" in json.dumps(c).lower() or "email" in c[1]])

    def test_without_the_sync_yes_it_says_how_to_see_every_agent(self):
        self.server = update(agents=False, missed=False)
        ctx = self.prompt("DDS update")
        self.assertIn("type **datastash sync** in this agent's session", ctx)

    def test_a_failed_read_says_so(self):
        self.server = None
        ctx = self.prompt("DDS update")
        self.assertIn("**Open list:** datastash didn't answer just now", ctx)

    def test_other_prompts_are_not_taken(self):
        for text in ("DDS update please fix the build", "update dds", "show asks"):
            self.assertNotEqual(ds.onboard_phrase(text), "dds_update", text)


class Auto(DdsBase):
    def test_the_6am_post_ends_with_exactly_the_one_line_and_is_recorded(self):
        rc, out, err = self.run_cmd(ds.cmd_dds_update, ["--auto"])
        self.assertEqual(rc, 0, err)
        lines = out.strip().split("\n")
        self.assertEqual(lines[0], "**DDS update**")
        self.assertEqual(lines[-1], "Type **DDS update** to get this any time.")
        self.assertEqual(out.count("DDS update** to get this"), 1)
        self.assertNotIn(ds.DDS_DELIVER_QUESTION, out)
        self.assertEqual(len(self.sent("POST", ds.DDS_UPDATE_PATH + "/posted")), 1)

    def test_no_second_post_the_same_day(self):
        self.server = update(week={"posted_today": True})
        rc, out, _ = self.run_cmd(ds.cmd_dds_update, ["--auto"])
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.sent("POST", ds.DDS_UPDATE_PATH + "/posted"), [])

    def test_after_the_seven_days_the_wake_is_removed_and_nothing_is_posted(self):
        ds.write_private(ds._dds_week_path("web-app"), json.dumps({"timer_id": "11111111-2222-3333-4444-555555555555",
                                                                    "workbook_id": "wb-1"}))
        self.server = update(week={"day": None, "auto": False})
        rc, out, _ = self.run_cmd(ds.cmd_dds_update, ["--auto"])
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(len([c for c in self.calls if c[0] == "DELETE"]), 1)
        self.assertTrue(ds.read_json(ds._dds_week_path("web-app"), {}).get("stopped_at"))
        self.run_cmd(ds.cmd_dds_update, ["--auto"])
        self.assertEqual(len([c for c in self.calls if c[0] == "DELETE"]), 1)  # once

    def test_a_typed_run_has_no_ending_line(self):
        rc, out, _ = self.run_cmd(ds.cmd_dds_update, [])
        self.assertEqual(rc, 0)
        self.assertNotIn(ds.DDS_UPDATE_ENDING, out)
        self.assertEqual(self.sent("POST", ds.DDS_UPDATE_PATH + "/posted"), [])


class Setup(DdsBase):
    def agent(self):
        return {"name": "web-app", "slug": "web-app", "workbook_id": "wb-1"}

    def test_one_daily_6am_central_timer_once_per_agent(self):
        tid = ds.dds_week_setup(self.agent(), "ldgr_ag_test")
        self.assertEqual(tid, "11111111-2222-3333-4444-555555555555")
        sets = self.sent("POST", ds.AGENT_TIMERS_PATH)
        self.assertEqual(len(sets), 1)
        body = sets[0][3]
        self.assertEqual((body["at"], body["tz"], body["repeat"], body["workbook_id"]), ("06:00", "America/Chicago", "daily", "wb-1"))
        self.assertTrue(body["note"].startswith("DDS update (6 AM)"))
        self.assertEqual(len(self.sent("POST", ds.DDS_UPDATE_PATH + "/start")), 1)
        self.assertIsNone(ds.dds_week_setup(self.agent(), "ldgr_ag_test"))  # kept: never again
        self.assertEqual(len(self.sent("POST", ds.AGENT_TIMERS_PATH)), 1)

    def test_a_timer_already_there_is_reused(self):
        self.timers = [{"timer_id": "99999999-2222-3333-4444-555555555555", "note": ds.DDS_WAKE_NOTE}]
        self.assertEqual(ds.dds_week_setup(self.agent(), "ldgr_ag_test"), "99999999-2222-3333-4444-555555555555")
        self.assertEqual(self.sent("POST", ds.AGENT_TIMERS_PATH), [])


if __name__ == "__main__":
    unittest.main()
