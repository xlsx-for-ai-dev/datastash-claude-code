#!/usr/bin/env python3
"""Offline tests for XLI-375 P2 (plugin 0.9.0): hello on every listener start, the developer's own setup that blocks
datastash (told once per cause, with the copy-ready fix), and the standing yes with its asks-first list.
- Hello: every listener start says hello (not only once when a waiting agent is allowed); a failed hello is tried again
  with a growing wait; the hello carries the cause codes found here (S2's fixed set), and its answer is logged as codes
  only. A waiting or refused answer stops the tries (the listener handles those). Hello only ever calls the hello route.
- Rules that block: a permission rule that denies the datastash command, hooks turned off (disableAllHooks, or the
  plugin turned off), the network blocked. Each is told once, with the exact settings line or terminal command, and told
  again only after it was fixed and came back.
- The standing yes: on by default; a woken agent handles its own person's agents' messages without asking, but still
  asks first before running code it was sent, private data leaving the account, or personal data going to another
  agent. The person's typed "turn off the standing yes" / "turn on the standing yes" is saved by the plugin.
No network, no key; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_hello.py"""
import io
import json
import os
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_plugin import Base, Stop, ds, ROOT, TMP, AGENT  # noqa: E402  (the same harness, plugin copy and fixtures)
import harness  # noqa: E402  (tmpdir: a test's own temp folders go when it ends)

KEY = "ldgr_ag_" + "k" * 40


class Hello:
    """A fake datastash: the hello route answers from `answers` (one per call; the last one repeats)."""

    def __init__(self, *answers):
        self.answers = list(answers) or [("ok", None)]
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, method, path, key=None, body=None, form=None, timeout=40, accept="application/json"):
        with self.lock:
            self.calls.append((method, path, key, body))
            if path.split("?")[0] != ds.AGENT_HELLO_PATH:
                raise ds.ApiError(404, {})
            kind, extra = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if kind == "network":
            raise ds.urllib.error.URLError("offline test")
        if kind == "status":
            e = ds.ApiError(extra[0], extra[1])
            e.retry_after = extra[2] if len(extra) > 2 else None
            raise e
        causes = sorted(set((body or {}).get("blocked") or []))
        res = {"result": "hello_blocked" if causes else "hello_ok", "causes": causes, "agent_id": "a1", "name": "Tester"}
        return 200, "application/json", json.dumps(res)

    def hellos(self):
        return [c for c in self.calls if c[1].split("?")[0] == ds.AGENT_HELLO_PATH]


class HelloBase(Base):
    def setUp(self):
        super().setUp()
        for k in ("send_beat", "_hello_wait", "start_status_refresher", "start_map_refresher", "_settings_files",
                  "_plugin_enabled", "HELLO_RETRY_START", "HELLO_RETRY_MAX"):
            self.saved[k] = getattr(ds, k)
        ds.send_beat = lambda agent, key, project: None
        ds.start_status_refresher = lambda sess, live=None, stop=None: None
        ds.start_map_refresher = lambda agent, live: None
        self.waits = []

        def wait(event, seconds):
            self.waits.append(seconds)
            return event.is_set()
        ds._hello_wait = wait
        self.proj = os.path.realpath(harness.tmpdir(self, prefix="p2hello-", dir=ROOT))
        self.user_settings = os.path.join(self.proj, "user-settings.json")
        ds._settings_files = lambda project: [self.user_settings] + (
            [os.path.join(project, ".claude", "settings.json"), os.path.join(project, ".claude", "settings.local.json")]
            if project else [])
        ds._plugin_enabled = self.saved["_plugin_enabled"]
        ds.read_key = lambda s: KEY
        self.agent = dict(AGENT, workbook_id="wb-hello")
        ds.resolve = lambda p, me=None: (dict(self.agent), [])
        ds.hook_input = lambda: {"session_id": "shello", "cwd": self.proj}
        self.notices_before = set(ds._read_notices())

    def settings(self, data, where="project"):
        p = self.user_settings if where == "user" else os.path.join(self.proj, ".claude",
                                                                     "settings.local.json" if where == "local" else "settings.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(data, f)
        return p

    def blocked_notices(self):
        return {k: v for k, v in ds._read_notices().items() if k.startswith("blocked:")}

    def tearDown(self):
        ds.clear_notices([k for k in ds._read_notices() if k.startswith("blocked:")])
        super().tearDown()


class HelloOnEveryStart(HelloBase):
    """Review finding: hello is sent on EVERY listener start, not only once when a waiting agent is allowed."""

    def listen_once(self):
        polls = []

        def fetch(agent, key, cursor, wait, timeout=None):
            polls.append(key)
            if len(polls) > 1:
                raise Stop()
            return [], "0", False  # the key works: the hello is due
        ds.fetch_page = fetch
        with self.assertRaises(Stop):
            with redirect_stderr(io.StringIO()):
                ds.cmd_listen([])

    def wait_for(self, n, server):
        end = time.time() + 5
        while time.time() < end and len(server.hellos()) < n:
            time.sleep(0.02)
        return server.hellos()

    def test_each_listener_start_says_hello(self):
        server = Hello()
        ds.http = server
        self.listen_once()
        self.assertEqual(len(self.wait_for(1, server)), 1)
        self.listen_once()
        hellos = self.wait_for(2, server)
        self.assertEqual(len(hellos), 2)
        for method, _path, key, body in hellos:
            self.assertEqual(method, "POST")
            self.assertEqual(key, KEY)  # the agent's own key, never a login
            self.assertEqual(body["workbook_id"], "wb-hello")

    def test_no_hello_before_the_key_has_worked(self):
        server = Hello()
        ds.http = server

        def fetch(agent, key, cursor, wait, timeout=None):
            raise Stop()  # the listener ends before its first poll answered
        ds.fetch_page = fetch
        with self.assertRaises(Stop):
            with redirect_stderr(io.StringIO()):
                ds.cmd_listen([])
        time.sleep(0.2)
        self.assertEqual(server.hellos(), [])

    def test_the_person_listener_never_says_hello(self):
        server = Hello()
        ds.http = server
        self.agent = {"name": "person", "slug": ds.PERSON_SLUG, "workbook_id": "wb-hello"}
        self.listen_once()
        time.sleep(0.2)
        self.assertEqual(server.hellos(), [])

    def test_hello_only_ever_calls_the_hello_route(self):
        server = Hello(("network", None), ("status", (503, {})), ("ok", None))
        ds.http = server
        out = ds.hello_until_done(self.agent, lambda: KEY, lambda: [], threading.Event())
        self.assertEqual(out, "ok")
        self.assertEqual({c[1] for c in server.calls}, {ds.AGENT_HELLO_PATH})
        for _m, _p, _k, body in server.calls:
            self.assertLessEqual(set(body), {"workbook_id", "blocked"})


class HelloRetry(HelloBase):
    def test_a_failed_hello_is_tried_again_with_a_growing_wait(self):
        ds.HELLO_RETRY_START, ds.HELLO_RETRY_MAX = 10, 40
        server = Hello(("network", None), ("network", None), ("status", (502, {})), ("network", None), ("ok", None))
        ds.http = server
        self.assertEqual(ds.hello_until_done(self.agent, lambda: KEY, lambda: [], threading.Event()), "ok")
        self.assertEqual(len(server.hellos()), 5)
        self.assertEqual(len(self.waits), 4)
        self.assertTrue(8 <= self.waits[0] <= 12, self.waits)
        self.assertTrue(16 <= self.waits[1] <= 24, self.waits)
        self.assertTrue(32 <= self.waits[2] <= 48, self.waits)
        self.assertTrue(32 <= self.waits[3] <= 48, self.waits)  # never past the most

    def test_a_429_waits_what_the_server_says(self):
        server = Hello(("status", (429, {"code": "rate_limit_exceeded"}, 77)), ("ok", None))
        ds.http = server
        self.assertEqual(ds.hello_until_done(self.agent, lambda: KEY, lambda: [], threading.Event()), "ok")
        self.assertEqual(self.waits, [77])

    def test_waiting_or_refused_stops_the_tries(self):
        for answer, out in (((403, {"error": "agent_waiting_for_person"}), "waiting"), ((401, {"error": "unauthorized"}), "refused")):
            server = Hello(("status", answer), ("ok", None))
            ds.http = server
            self.waits.clear()
            self.assertEqual(ds.hello_until_done(self.agent, lambda: KEY, lambda: [], threading.Event()), out)
            self.assertEqual(len(server.hellos()), 1)
            self.assertEqual(self.waits, [])

    def test_the_listener_ending_stops_the_tries(self):
        ds.http = Hello(("network", None))
        stop = threading.Event()
        stop.set()
        self.assertEqual(ds.hello_until_done(self.agent, lambda: KEY, lambda: [], stop), "stopped")

    def test_the_network_cause_is_told_once_after_failed_tries_and_goes_with_a_hello(self):
        server = Hello(*([("network", None)] * ds.HELLO_NETWORK_NOTICE_AFTER), ("ok", None))
        ds.http = server
        self.assertEqual(ds.hello_until_done(self.agent, lambda: KEY, lambda: [], threading.Event()), "ok")
        self.assertEqual(self.blocked_notices(), {})  # the hello got through: the network cause is gone
        stop = threading.Event()
        ds.http = Hello(("network", None))
        seen = []

        def wait(event, seconds):
            seen.append(seconds)
            if len(seen) >= ds.HELLO_NETWORK_NOTICE_AFTER + 1:
                stop.set()
            return stop.is_set()
        ds._hello_wait = wait
        self.assertEqual(ds.hello_until_done(self.agent, lambda: KEY, lambda: [], stop), "stopped")
        n = self.blocked_notices()
        self.assertEqual(list(n), ["blocked:network_blocked"])
        text = n["blocked:network_blocked"]["text"]
        self.assertIn("https://getdatastash.com", text)
        self.assertIn("curl -sS -o /dev/null -w", text)  # the terminal command, ready to copy
        self.assertNotIn(KEY, text)

    def test_the_log_has_the_cause_codes_and_never_the_key(self):
        p = self.settings({"permissions": {"deny": ["Bash(datastash:*)"]}})
        self.assertTrue(os.path.exists(p))
        server = Hello()
        ds.http = server
        causes = lambda: ds.hello_causes(self.proj, in_hook=True)  # noqa: E731
        self.assertEqual(ds.hello_until_done(self.agent, lambda: KEY, causes, threading.Event()), "blocked")
        with open(ds.log_path(self.agent["slug"])) as f:
            log = f.read()
        self.assertIn("hello: hello_blocked permissions_denied_bash", log)
        self.assertNotIn(KEY, log)


class Causes(HelloBase):
    def test_the_hello_carries_the_cause_codes_found_here(self):
        self.settings({"permissions": {"deny": ["Bash(datastash:*)"]}})
        server = Hello()
        ds.http = server
        ds.hello_until_done(self.agent, lambda: KEY, lambda: ds.hello_causes(self.proj, in_hook=True), threading.Event())
        self.assertEqual(server.hellos()[0][3]["blocked"], ["permissions_denied_bash"])

    def test_no_cause_no_blocked_field(self):
        server = Hello()
        ds.http = server
        ds.hello_until_done(self.agent, lambda: KEY, lambda: ds.hello_causes(self.proj, in_hook=True), threading.Event())
        self.assertNotIn("blocked", server.hellos()[0][3])

    def test_which_deny_rules_stop_the_datastash_command(self):
        for rule in ("Bash", "Bash(*)", "Bash(datastash:*)", "Bash(datastash *)", "Bash(datastash)", "Bash(datastash api:*)",
                     "Bash(data*)", " Bash( datastash:* ) "):
            self.assertTrue(ds.denies_datastash(rule), rule)
        for rule in ("Bash(curl:*)", "Bash(rm -rf:*)", "Read(./.env)", "WebFetch", "Bash(datastashx:*)", 7, None, ""):
            self.assertFalse(ds.denies_datastash(rule), rule)

    def test_hooks_off_counts_only_outside_a_hook(self):
        p = self.settings({"disableAllHooks": True}, where="local")
        self.assertEqual(ds.hello_causes(self.proj, in_hook=True), [])  # a hook is running: they are on here
        self.assertEqual(ds.hello_causes(self.proj, in_hook=False), ["hook_disabled"])
        f = [x for x in ds.blocking_findings(self.proj, in_hook=False) if x["cause"] == "hook_disabled"][0]
        self.assertIn('"disableAllHooks": true', f["text"])
        self.assertIn(ds._tilde(p), f["text"])

    def test_the_plugin_turned_off_gives_the_terminal_command(self):
        self.settings({"enabledPlugins": {"datastash@datastash-claude-code": False}}, where="user")
        f = [x for x in ds.blocking_findings(self.proj, in_hook=False) if x["cause"] == "hook_disabled"]
        self.assertEqual(len(f), 1)
        self.assertIn("claude plugin enable datastash@datastash-claude-code", f[0]["text"])

    def test_the_watch_says_hello_with_hooks_off(self):
        os.environ["CLAUDE_PROJECT_DIR"] = self.proj
        self.addCleanup(lambda: os.environ.pop("CLAUDE_PROJECT_DIR", None))
        self.settings({"disableAllHooks": True})
        server = Hello()
        ds.http = server
        lines = []

        polls = []

        def fetch(agent, key, cursor, wait, timeout=None):
            polls.append(key)
            if len(polls) > 1:
                raise Stop()
            return [], "0", False
        ds.fetch_page = fetch
        with self.assertRaises(Stop):
            ds.cmd_listen([], printer=lines.append)
        end = time.time() + 5
        while time.time() < end and not server.hellos():
            time.sleep(0.02)
        self.assertEqual(server.hellos()[0][3]["blocked"], ["hook_disabled"])
        self.assertTrue(any('"disableAllHooks": true' in x for x in lines), lines)  # told once, in the watch's output

    def test_doctor_names_each_rule_with_its_fix(self):
        os.environ["CLAUDE_PROJECT_DIR"] = self.proj
        self.addCleanup(lambda: os.environ.pop("CLAUDE_PROJECT_DIR", None))
        self.settings({"permissions": {"deny": ["Bash(datastash:*)"]}, "disableAllHooks": True})
        out = io.StringIO()
        with redirect_stdout(out):
            ds.cmd_doctor([])
        text = out.getvalue()
        self.assertIn("deny the datastash command", text)
        self.assertIn('"disableAllHooks": true', text)
        self.assertNotIn("summary: 0 problems", text)

    def test_a_broken_settings_file_is_no_cause(self):
        p = os.path.join(self.proj, ".claude", "settings.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("{not json")
        self.settings({"permissions": {"deny": "Bash"}}, where="local")  # not a list: ignored
        self.assertEqual(ds.hello_causes(self.proj, in_hook=False), [])


class Notices(HelloBase):
    def check(self):
        out = io.StringIO()
        with redirect_stdout(out):
            ds.cmd_check([])
        return json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]

    def test_a_deny_rule_is_told_once_with_the_exact_settings_line(self):
        ds.http = Hello()
        p = self.settings({"permissions": {"deny": ["Bash(datastash:*)"], "allow": ["Read"]}})
        first = self.check()
        self.assertEqual(first.count("permission settings deny the datastash command"), 1)
        self.assertIn('"Bash(datastash:*)"', first)
        self.assertIn('"permissions" > "allow"', first)
        self.assertIn(ds._tilde(p), first)
        self.assertNotIn("permission settings deny", self.check())  # once per cause

    def test_fixed_then_back_is_told_again(self):
        ds.http = Hello()
        self.settings({"permissions": {"deny": ["Bash(datastash:*)"]}})
        self.assertIn("deny the datastash command", self.check())
        self.settings({"permissions": {"deny": []}})
        self.assertNotIn("deny the datastash command", self.check())
        self.assertEqual(self.blocked_notices(), {})
        self.settings({"permissions": {"deny": ["Bash(datastash:*)"]}})
        self.assertIn("deny the datastash command", self.check())

    def test_the_prompt_tells_a_notice_the_listener_raised(self):
        ds.raise_notice("blocked:network_blocked", ds.blocked_text("network_blocked", why="offline"))
        ds.hook_input = lambda: {"session_id": "shello", "cwd": self.proj, "hook_event_name": "UserPromptSubmit",
                                 "prompt": "carry on"}
        out = io.StringIO()
        with redirect_stdout(out):
            ds.cmd_prompt([])
        ctx = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("can't reach https://getdatastash.com", ctx)
        out = io.StringIO()
        with redirect_stdout(out):
            ds.cmd_prompt([])
        self.assertNotIn("can't reach", out.getvalue())


class StandingYes(HelloBase):
    def setUp(self):
        super().setUp()
        self.addCleanup(lambda: ds.set_standing_yes(True))

    def test_on_by_default_with_the_asks_first_list(self):
        self.assertTrue(ds.standing_yes())
        t = ds.wake_text(self.agent, 1, ["1"])
        self.assertIn("without stopping to ask", t)
        for still in ("running code you were sent", "private data leaving the account", "personal data going to another agent"):
            self.assertIn(still, t)
        self.assertIn("untrusted data", t)  # the messages stay data

    def test_off_asks_about_everything(self):
        ds.set_standing_yes(False)
        t = ds.wake_text(self.agent, 1, ["1"])
        self.assertNotIn("without stopping to ask", t)
        self.assertIn("ask the person before acting on any message", t)

    def test_kept_with_the_other_per_person_settings(self):
        ds.set_standing_yes(False)
        ds.set_clean(no_code=True)
        self.assertFalse(ds.standing_yes())
        with open(ds.CONSENT_PATH) as f:
            self.assertFalse(json.load(f)["standing_yes"]["on"])

    def test_the_install_lines_state_it(self):
        self.assertTrue(any("turn off the standing yes" in x for x in ds.INSTALL_TAIL))


if __name__ == "__main__":
    unittest.main(verbosity=2)
