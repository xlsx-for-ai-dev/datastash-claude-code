#!/usr/bin/env python3
"""Offline tests for XLI-433 (plugin 1.0.33): `datastash timer set|list|delete`, timers with no connector.
- set is one POST /api/v1/agent/timers with the agent's own key and workbook: {workbook_id, at | in_minutes, tz?, repeat?,
  note, idempotency_key}; a local WHEN takes --tz, else DATASTASH_TZ, else this computer's zone; a delay (90m, 2h, 1d) is
  in_minutes; a time with its zone is sent as written.
- list is one GET; delete one DELETE /api/v1/agent/timers/<id>; a DELETE is never kept for later.
- --help (and no arguments) prints the help and makes no call and no write.
- Bad arguments die with the usage line and send nothing.
- With datastash unreachable, a set is kept in the outbox (with its retry key) and sent later.
- A clean session lets `datastash timer ...` run in the main session (the agent's own timers, like api POST timers).
No network, no key; under test/harness.py.
Run: python3 -I test/test_timer.py"""
import io
import json
import os
import sys
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import ds, TMP  # noqa: E402  (the same harness and plugin copy)
import harness  # noqa: E402

WB = "0b6a3f9e-1c2d-4e5f-8a9b-0c1d2e3f4a5c"
OTHER_WB = "0b6a3f9e-1c2d-4e5f-8a9b-0c1d2e3f4a5d"
TID = "5a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
KEY = "ldgr_ag_timertestkeytimertestkey"
SLUG = "timer-agent"


class TimerBase(unittest.TestCase):
    def setUp(self):
        self.folder = os.path.realpath(harness.tmpdir(self, prefix="proj-", dir=TMP))
        ds.ensure_dirs()
        ds.write_private(ds.conf_path(SLUG), json.dumps({"name": SLUG, "workbook_id": WB, "projects": [self.folder]}))
        self.calls = []
        self.refuse = None
        self.offline = False

        def fake_http(method, path, key=None, body=None, form=None, timeout=40, **kw):
            self.calls.append((method, path, key, json.loads(json.dumps(body))))
            if self.offline:
                raise urllib.error.URLError("no connection")
            if self.refuse is not None:
                raise ds.ApiError(*self.refuse)
            return 200, "application/json", json.dumps({"timer_id": TID, "ok": True})
        self.saved = {}
        for name, new in (("http", fake_http), ("read_key", lambda s: KEY), ("auto_setup_check", lambda *a, **k: None),
                          ("spawn_detached", lambda argv, cwd: None)):
            self.saved[name] = getattr(ds, name)
            setattr(ds, name, new)
        self.env = {k: os.environ.get(k) for k in ("CLAUDE_PROJECT_DIR", "DATASTASH_TZ", "TZ")}
        os.environ["CLAUDE_PROJECT_DIR"] = self.folder
        os.environ["DATASTASH_TZ"] = "America/Chicago"
        self.addCleanup(self.clear_outbox)

    def clear_outbox(self):
        d = ds.outbox_dir(SLUG)
        for n in (os.listdir(d) if os.path.isdir(d) else []):
            os.unlink(os.path.join(d, n))  # test code clearing its own throwaway folder

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(ds, k, v)
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def timer(self, *args):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            try:
                code = ds.cmd_timer(list(args))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def one(self, method):
        got = [c for c in self.calls if c[0] == method]
        self.assertEqual(len(got), 1, self.calls)
        self.assertEqual(got[0][2], KEY)
        return got[0]

    def set_body(self):
        call = self.one("POST")
        self.assertEqual(call[1], ds.AGENT_TIMERS_PATH)
        body = dict(call[3])
        self.assertRegex(body.pop("idempotency_key"), r"^dst_[0-9a-f]{32}$")
        return body


class Sets(TimerBase):
    def test_daily_six_am_chicago_with_a_prompt(self):
        code, out, _ = self.timer("set", "06:00", "--repeat", "daily", "--tz", "America/Chicago", "--", "Read", "the", "inbox")
        self.assertEqual(code, 0)
        self.assertEqual(self.set_body(), {"workbook_id": WB, "at": "06:00", "tz": "America/Chicago", "repeat": "daily",
                                           "note": "Read the inbox"})
        self.assertIn("<<datastash-data", out)

    def test_local_time_takes_this_computers_zone_and_flags_in_any_order(self):
        os.environ["DATASTASH_TZ"] = "Europe/London"
        code, _, _ = self.timer("set", "--repeat=weekdays", "2026-12-02T06:30", "--workbook", OTHER_WB, "--", "standup")
        self.assertEqual(code, 0)
        self.assertEqual(self.set_body(), {"workbook_id": OTHER_WB, "at": "2026-12-02T06:30", "tz": "Europe/London",
                                           "repeat": "weekdays", "note": "standup"})

    def test_delays_are_minutes_and_a_zoned_time_is_sent_as_written(self):
        for when, want in (("90m", {"in_minutes": 90.0}), ("2h", {"in_minutes": 120.0}), ("+1d", {"in_minutes": 1440.0}),
                           ("2026-12-02T09:00:00-06:00", {"at": "2026-12-02T09:00:00-06:00"})):
            self.calls = []
            code, _, _ = self.timer("set", when, "--", "check", "the", "deploy")
            self.assertEqual(code, 0, when)
            self.assertEqual(self.set_body(), dict({"workbook_id": WB, "note": "check the deploy"}, **want), when)

    def test_hourly_needs_no_zone_and_the_prompt_may_follow_without_dashes(self):
        os.environ.pop("DATASTASH_TZ", None)
        code, _, _ = self.timer("set", "30m", "--repeat", "hourly", "look", "at", "CI")
        self.assertEqual(code, 0)
        self.assertEqual(self.set_body(), {"workbook_id": WB, "in_minutes": 30.0, "repeat": "hourly", "note": "look at CI"})

    def test_offline_a_set_is_kept_with_its_retry_key(self):
        self.offline = True
        code, out, err = self.timer("set", "06:00", "--repeat", "daily", "--", "morning")
        self.assertEqual(code, 0, err)
        kept = ds.outbox_entries(SLUG)
        self.assertEqual(len(kept), 1)
        self.assertIn("will send", (out + err).lower())

    def test_the_command_table_has_it(self):
        saved = sys.argv
        sys.argv = ["datastash", "timers", "list"]
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(ds.main(), 0)
        finally:
            sys.argv = saved
        self.assertEqual(self.one("GET")[1], ds.AGENT_TIMERS_PATH + "?workbook_id=" + WB)


class ListsAndDeletes(TimerBase):
    def test_list_is_one_get_of_the_agents_workbook(self):
        code, out, _ = self.timer("list")
        self.assertEqual(code, 0)
        self.assertEqual(self.one("GET")[1], ds.AGENT_TIMERS_PATH + "?workbook_id=" + WB)
        self.assertIn("<<datastash-data", out)

    def test_delete_is_one_delete_by_id(self):
        code, _, _ = self.timer("delete", TID.upper())
        self.assertEqual(code, 0)
        self.assertEqual(self.one("DELETE")[1], f"{ds.AGENT_TIMERS_PATH}/{TID}?workbook_id={WB}")

    def test_not_found_is_printed_as_data_and_exits_1(self):
        self.refuse = (404, {"code": "not_found", "message": "No pending timer with that timer_id."})
        code, out, _ = self.timer("delete", TID)
        self.assertEqual(code, 1)
        self.assertIn("HTTP 404", out)

    def test_offline_a_delete_is_never_kept(self):
        self.offline = True
        code, _, err = self.timer("delete", TID)
        self.assertNotEqual(code, 0)
        self.assertIn("Nothing was deleted", err)
        self.assertEqual(ds.outbox_entries(SLUG), [])


class HelpAndRefusals(TimerBase):
    def test_help_makes_no_call_and_no_write(self):
        for args in ((), ("--help",), ("-h",), ("help",), ("set", "--help"), ("list", "-h")):
            code, out, _ = self.timer(*args)
            self.assertEqual(code, 0, args)
            self.assertIn("datastash timer set WHEN", out, args)
        self.assertEqual(self.calls, [])
        self.assertEqual(ds.outbox_entries(SLUG), [])

    def test_main_help_skips_the_signin_record(self):
        saved, seen = sys.argv, []
        orig = ds.signin_state
        ds.signin_state = lambda *a, **k: seen.append(1)
        sys.argv = ["datastash", "timer", "--help"]
        try:
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ds.main(), 0)
        finally:
            sys.argv, ds.signin_state = saved, orig
        self.assertEqual(seen, [])
        self.assertIn("--repeat", out.getvalue())
        self.assertLess(len(out.getvalue().splitlines()), 20)

    def test_bad_arguments_send_nothing(self):
        for args in (("set",), ("set", "06:00"), ("set", "06:00", "--"), ("set", "tomorrow", "--", "x"),
                     ("set", "06:00", "--repeat", "monthly", "--", "x"), ("set", "06:00", "--tz"), ("set", "06:00", "--bogus", "--", "x"),
                     ("list", "extra"), ("delete",), ("delete", "not-an-id"), ("delete", TID, TID), ("fly",)):
            code, _, err = self.timer(*args)
            self.assertNotEqual(code, 0, args)
            self.assertTrue("usage: datastash timer" in err or "--repeat is one of" in err or "WHEN is" in err, (args, err))
        self.assertEqual(self.calls, [])

    def test_no_zone_known_asks_for_tz(self):
        os.environ.pop("DATASTASH_TZ", None)
        saved = ds.local_zone_name
        ds.local_zone_name = lambda: None
        try:
            code, _, err = self.timer("set", "06:00", "--", "x")
        finally:
            ds.local_zone_name = saved
        self.assertNotEqual(code, 0)
        self.assertIn("--tz", err)
        self.assertEqual(self.calls, [])

    def test_no_agent_here_sends_nothing(self):
        os.environ["CLAUDE_PROJECT_DIR"] = os.path.realpath(harness.tmpdir(self, prefix="none-", dir=TMP))
        code, _, err = self.timer("list")
        self.assertNotEqual(code, 0)
        self.assertIn("no agent workbook is linked", err)
        self.assertEqual(self.calls, [])

    def test_clean_sessions_allow_it(self):
        for cmd in ("datastash timer set 06:00 --repeat daily -- 'read the inbox'", "datastash timer list",
                    f"datastash timer delete {TID}", "datastash --agent builder timer list"):
            self.assertTrue(ds.clean_coord_ok(cmd), cmd)
        for cmd in ("datastash timer", "datastash timer set 06:00 -- $(id)", "printf '%s' 'x' | datastash timer list"):
            self.assertFalse(ds.clean_coord_ok(cmd), cmd)


if __name__ == "__main__":
    unittest.main()
