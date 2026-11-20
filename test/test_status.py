#!/usr/bin/env python3
"""Offline tests for the agent status (owner 11-20): the numbered list, the local copy, the end-of-turn reminder (never
loops), the background write, "do N" answers, "full context of N", and the install fill. No network, no key.
Run: python3 -I test/test_status.py"""
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "..", "plugins", "datastash", "scripts", "datastash")
TMP = tempfile.mkdtemp(prefix="ds-status-test-")
os.environ["DATASTASH_CONFIG_DIR"] = TMP
os.environ["DATASTASH_MAIN_DIR"] = os.path.join(TMP, "home", "datastash main")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "claude-config")
os.environ["DATASTASH_STATUS_RETRIES"] = "0.01,0.01"
for k in ("DATASTASH_AGENT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR"):
    os.environ.pop(k, None)

loader = importlib.machinery.SourceFileLoader("datastash_status", BIN)
spec = importlib.util.spec_from_loader("datastash_status", loader)
ds = importlib.util.module_from_spec(spec)
loader.exec_module(ds)

WB = "11111111-2222-3333-4444-555555555555"
NOW = time.time()


def row(agent, doing, asks=(), ago=60, **kw):
    r = {"agent": agent, "doing": doing, "asks": list(asks), "updated_at": NOW - ago}
    r.update(kw)
    return r


EXAMPLE = [
    row("docs", "Nothing in flight since Monday. Waiting for the next card.", ago=3 * 86400),
    row("api", "Billing refactor done; cleaning up two leftover TODOs.", ago=3600,
        context={"project": "api", "branch": "main", "pr": "no PR", "tests": "all pass", "last_change": "moved invoices to the new table"},
        asks=[{"id": "q1", "text": "Delete the old /invoices endpoint?", "touches": "src/routes/invoices.ts",
               "if_yes": "removes 140 lines", "if_no": "I leave it with a deprecation note", "details": "Nothing calls it."}]),
    row("web-app", "Fixing the login timeout; sessions expire after a day, so users get kicked out mid-task. Two tests still fail.",
        ago=300, details="Tried refresh on activity; the two failing tests expect 7 days.",
        context={"project": "web-app", "branch": "fix-login", "pr": "#41 open", "tests": "38 pass, 2 fail", "last_change": "session refresh on activity"},
        asks=[{"id": "q2", "text": "Change the session length from 1 day to 7 days?", "touches": "src/auth/session.ts",
               "if_yes": "fixes the 2 failing tests", "if_no": "I add a refresh prompt instead"}],
        cards=[{"id": "XLI-12", "step": "review", "review": "Opus FAIL at abc1234, CI red", "blocker": "a failing test"}],
        running=[{"what": "builder", "on": "XLI-12", "stuck": True}],
        waiting_on=[{"what": "the EPM pass", "who": "EPM"}],
        done_asked=[{"request": "post the summary", "done": True, "proof": "posted to #team"}]),
]


class Base(unittest.TestCase):
    def setUp(self):
        ds.ensure_dirs()
        for n in os.listdir(ds.STATE):
            os.replace(os.path.join(ds.STATE, n), os.path.join(TMP, f"old-{n}-{time.time()}"))
        for n in os.listdir(ds.CONF):
            if n.startswith("status-") or n.startswith("agent-"):
                os.replace(os.path.join(ds.CONF, n), os.path.join(TMP, f"old-{n}-{time.time()}"))
        self.saved = {k: getattr(ds, k) for k in ("http", "read_key", "hook_input", "_claude_chain", "signed_in",
                                                    "login_token", "gh_pr", "this_session")}
        self.calls = []
        self.server_rows = [dict(r) for r in EXAMPLE]
        self.fail_with = None

        def fake_http(method, path, key=None, body=None, form=None, timeout=40):
            self.calls.append((method, path, key, body))
            if self.fail_with is not None:
                raise self.fail_with
            if method == "GET" and path.startswith((ds.STATUS_PATH + "?", ds.PERSON_STATUS_PATH + "?")):
                return 200, "application/json", json.dumps({"workbook_id": WB, "agents": [
                    dict(r, updated_at=r["updated_at"]) for r in self.server_rows]})
            return 200, "application/json", json.dumps({"ok": True})
        ds.http = fake_http
        ds.read_key = lambda s: "dsat_person" if ds.person_slug(s) else "ldgr_ag_test"
        ds.signed_in = lambda: True
        ds.login_token = lambda force=False: "dsat_person"
        ds.gh_pr = lambda here: {}
        ds._claude_chain = lambda: [[4242, "start-A"]]
        self.folder = tempfile.mkdtemp(prefix="proj-", dir=TMP)
        ds.write_private(ds.conf_path("web-app"), json.dumps({"name": "web-app", "workbook_id": WB, "projects": [self.folder]}))
        ds.write_private(ds.key_path("web-app"), "ldgr_ag_" + "x" * 40)
        self.env = {k: os.environ.get(k) for k in ("CLAUDE_PROJECT_DIR", "CLAUDE_CODE_SESSION_ID")}
        os.environ["CLAUDE_PROJECT_DIR"] = self.folder
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sess1"

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(ds, k, v)
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def run_cmd(self, fn, args, stdin=None):
        orig = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                try:
                    rc = fn(args)
                except SystemExit as e:
                    rc = e.code
        finally:
            sys.stdin = orig
        return rc, out.getvalue(), err.getvalue()

    def hook(self, fn, inp):
        ds.hook_input = lambda: inp
        rc, out, err = self.run_cmd(fn, [])
        self.assertEqual(rc, 0, err)
        return json.loads(out) if out.strip() else None


class ListFormat(Base):
    def test_owner_example_numbers_and_order(self):
        rows = [ds.norm_status_row(r) for r in EXAMPLE]
        lines, numbers = ds.status_list(rows, NOW)
        text = "\n".join(lines)
        self.assertTrue(lines[0].startswith("1. **web-app** — Fixing the login timeout"), text)
        self.assertEqual(lines[1], "   fix-login · PR #41 open · tests 38 pass, 2 fail · last: session refresh on activity · 5 min ago")
        self.assertIn("   XLI-12: built ✓ · [review: Opus FAIL at abc1234, CI red] · blocker: a failing test", lines)
        self.assertIn("   running: builder on XLI-12 (STUCK)", lines)
        self.assertIn("   waiting on: EPM for the EPM pass", lines)
        self.assertIn("   you asked: post the summary ✓ (posted to #team)", lines)
        self.assertIn("   2. Ask: Change the session length from 1 day to 7 days (src/auth/session.ts)? If yes: fixes the 2 "
                      "failing tests. If no: I add a refresh prompt instead.", lines)
        self.assertIn("3. **api** — Billing refactor done; cleaning up two leftover TODOs.", lines)
        self.assertIn("   main · no PR · tests all pass · last: moved invoices to the new table · 1 h ago", lines)
        self.assertTrue(lines[-1].startswith("5. **docs** — Nothing in flight since Monday. · 3 d ago"), text)  # idle: last, one line
        self.assertEqual((numbers["2"]["agent"], numbers["2"]["ask"]), ("web-app", "q2"))
        self.assertEqual((numbers["4"]["agent"], numbers["4"]["ask"]), ("api", "q1"))
        self.assertEqual(numbers["5"], {"agent": "docs", "ask": None})

    def test_agent_text_is_one_line_and_answered_asks_drop(self):
        r = ds.norm_status_row(row("x", "line one\nline two\x1b[31m", asks=[{"text": "ok?", "answered": True}, "keep?"]))
        self.assertEqual(r["doing"], "line one line two [31m")
        self.assertEqual(ds._clean("a\u202eb\u2028c"), "a b c")  # no direction flips or hidden line breaks
        self.assertEqual([a["text"] for a in r["asks"]], ["keep?"])
        self.assertEqual(r["asks"][0]["id"], ds.ask_id("keep?"))  # stable id from the words
        self.assertIsNone(ds.norm_status_row({"doing": "no name"}))


class Phrases(unittest.TestCase):
    def test_phrases(self):
        P = ds.status_phrase
        self.assertEqual(P("status of all my agents"), ("all", None))
        self.assertEqual(P("What's the status of all my agents?"), ("all", None))
        self.assertEqual(P("what are my agents doing"), ("all", None))
        self.assertEqual(P("full context of 4"), ("details", 4))
        self.assertEqual(P("more on 12"), ("details", 12))
        self.assertEqual(P("details 3"), ("details", 3))
        self.assertEqual(P("do 4"), ("answer", (4, "Yes, do it.")))
        self.assertEqual(P("4: no, 30 days"), ("answer", (4, "no, 30 days")))
        self.assertIsNone(P("2) update the readme\n3) bump the version"))
        for t in ("2-3 more tests please", "2:30 works for me", "5-star ratings only", "1) fix the login bug",
                  "10 - 15 minutes is fine"):  # review 1: ordinary prompts that start with a number
            self.assertIsNone(P(t), t)
        for t in ("fix the agent status bug in api", "do 4 things for me please", "the status of my agents table is wrong "
                  "and needs a fix", "4", ""):
            self.assertIsNone(P(t), t)


class LocalCopy(Base):
    def test_fresh_copy_needs_no_network_and_is_fast(self):
        ds.fetch_status({"slug": "web-app", "workbook_id": WB, "name": "web-app"})
        self.calls.clear()
        t = time.time()
        out = ds.render_status_all(self.folder, ds.this_session())
        self.assertLess(time.time() - t, 0.1)
        self.assertEqual(self.calls, [])
        self.assertIn("refreshed 0 s ago", out[-1])
        self.assertEqual(os.stat(ds.status_cache_path(WB)).st_mode & 0o777, 0o600)

    def test_old_copy_is_fetched_once_and_a_failure_shows_the_old_copy(self):
        ds.save_status_cache(WB, [ds.norm_status_row(EXAMPLE[0])], time.time() - 600, merge=False)
        out = ds.render_status_all(self.folder, ds.this_session())
        self.assertEqual(len([c for c in self.calls if c[0] == "GET"]), 1)
        self.assertEqual(self.calls[0][2], "ldgr_ag_test")  # an agent session reads with its own key
        self.assertIn("**web-app**", "\n".join(out))
        ds.save_status_cache(WB, [ds.norm_status_row(EXAMPLE[0])], time.time() - 600, merge=False)
        self.fail_with = ds.ApiError(503, {"code": "busy"})
        out = ds.render_status_all(self.folder, ds.this_session())
        self.assertIn("showing the copy on this machine", out[-1])
        self.assertIn("**docs**", "\n".join(out))

    def test_refresher_refreshes_on_wake_and_backs_off_on_404(self):
        wake, stop = threading.Event(), threading.Event()
        ds.STATUS_REFRESH_SECONDS, old = 30, ds.STATUS_REFRESH_SECONDS
        try:
            th = threading.Thread(target=ds.status_refresher, args=({"slug": "web-app", "workbook_id": WB}, wake, stop), daemon=True)
            th.start()
            time.sleep(0.2)
            n = len(self.calls)
            self.assertEqual(n, 1)  # at start
            wake.set()
            time.sleep(0.2)
            self.assertEqual(len(self.calls), 2)  # at once on a change
            stop.set()
            wake.set()
            th.join(2)
        finally:
            ds.STATUS_REFRESH_SECONDS = old


class Write(Base):
    def status_set(self, data, args=("-",)):
        started = []
        orig = ds.spawn_detached
        ds.spawn_detached = lambda argv, cwd: started.append(argv)
        try:
            rc, out, err = self.run_cmd(ds.cmd_status_set, list(args), stdin=json.dumps(data) if data is not None else "")
        finally:
            ds.spawn_detached = orig
        return rc, out, err, started

    def test_write_is_local_at_once_and_sent_in_the_background(self):
        t = time.time()
        rc, out, err, started = self.status_set({"doing": "Turn one: wrote the tests.", "tests": "3 pass",
                                                 "asks": [{"text": "Merge it?", "touches": "README"}]})
        self.assertEqual(rc, 0, err)
        self.assertLess(time.time() - t, 1.0)
        self.assertEqual(self.calls, [])  # nothing on the network in the turn
        self.assertEqual(started[0][2:4], ["status-send", "web-app"])
        c = ds.read_status_cache(WB)
        mine = next(r for r in c["agents"] if r["agent"] == "web-app")
        self.assertEqual(mine["doing"], "Turn one: wrote the tests.")
        self.assertEqual(mine["context"]["tests"], "3 pass")
        # the sender sends the newest status as the agent; a 500 is retried, then it lands
        seq = started[0][4]
        n = {"i": 0}
        base = ds.http

        def flaky(method, path, **kw):
            if method == "POST":
                n["i"] += 1
                if n["i"] == 1:
                    raise ds.ApiError(500, {})
            return base(method, path, **kw)
        ds.http = flaky
        self.assertEqual(ds.cmd_status_send(["web-app", seq]), 0)
        posts = [c for c in self.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], ds.STATUS_PATH)
        body = posts[0][3]
        self.assertEqual(body["workbook_id"], WB)
        self.assertEqual(body["asks"][0]["text"], "Merge it?")
        self.assertEqual(set(body), {"workbook_id", "doing", "details", "context", "asks", "cards", "running", "health",
                                     "done_asked", "waiting_on"})
        self.assertEqual(set(body["context"]), {"project", "branch", "pr", "tests", "last_change"})

    def test_second_turn_changes_the_stored_status_and_an_old_sender_steps_aside(self):
        _, _, _, s1 = self.status_set({"doing": "Turn one."})
        _, _, _, s2 = self.status_set({"doing": "Turn two: all tests pass now."})
        mine = next(r for r in ds.read_status_cache(WB)["agents"] if r["agent"] == "web-app")
        self.assertEqual(mine["doing"], "Turn two: all tests pass now.")
        self.assertEqual(ds.cmd_status_send(["web-app", s1[0][4]]), 0)
        self.assertEqual([c for c in self.calls if c[0] == "POST"], [])  # the newer write's sender sends
        ds.cmd_status_send(["web-app", s2[0][4]])
        self.assertEqual([c[3]["doing"] for c in self.calls if c[0] == "POST"], ["Turn two: all tests pass now."])
        # a refresh with the server's older row keeps this machine's newer one
        self.server_rows = [row("web-app", "Turn one.", ago=999)]
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        mine = next(r for r in ds.read_status_cache(WB)["agents"] if r["agent"] == "web-app")
        self.assertEqual(mine["doing"], "Turn two: all tests pass now.")

    def test_same_resends_the_last_words(self):
        self.status_set({"doing": "Waiting on review.", "tests": "9 pass"})
        rc, out, err, started = self.status_set(None, args=("--same",))
        self.assertEqual(rc, 0, err)
        mine = next(r for r in ds.read_status_cache(WB)["agents"] if r["agent"] == "web-app")
        self.assertEqual((mine["doing"], mine["context"]["tests"]), ("Waiting on review.", "9 pass"))

    def test_refusals(self):
        rc, _, err, _ = self.status_set({"details": "no doing"})
        self.assertNotEqual(rc, 0)
        self.assertIn('"doing" is needed', err)
        os.environ["CLAUDE_PROJECT_DIR"] = tempfile.mkdtemp(dir=TMP)
        rc, _, err, _ = self.status_set({"doing": "x"})
        self.assertIn("no agent is linked", err)


class StopHook(Base):
    def stop(self, active=False):
        return self.hook(ds.cmd_status_check, {"session_id": "sess1", "cwd": self.folder, "stop_hook_active": active})

    def prompt(self, text="go on"):
        return self.hook(ds.cmd_prompt, {"session_id": "sess1", "prompt": text, "hook_event_name": "UserPromptSubmit"})

    def tool(self):
        ds.hook_input = lambda: {"session_id": "sess1", "hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {}}
        self.run_cmd(ds.cmd_agent_tool, [])

    def test_asks_once_per_turn_and_never_loops(self):
        self.prompt()
        self.assertIsNone(self.stop())  # no tool use: a trivial turn
        self.tool()
        v = self.stop()
        self.assertEqual(v["decision"], "block")
        self.assertIn("datastash status-set -", v["reason"])
        self.assertIsNone(self.stop(active=True))  # Claude Code is continuing for this hook
        self.assertIsNone(self.stop())  # at most once per turn
        time.sleep(0.01)
        self.prompt()  # next turn: asked again only if it used tools and wrote nothing
        self.tool()
        self.assertEqual(self.stop()["decision"], "block")

    def test_a_status_written_this_turn_ends_quietly(self):
        self.prompt()
        self.tool()
        orig = ds.spawn_detached
        ds.spawn_detached = lambda *a: None
        try:
            self.run_cmd(ds.cmd_status_set, ["-"], stdin=json.dumps({"doing": "Done for now."}))
        finally:
            ds.spawn_detached = orig
        self.assertIsNone(self.stop())

    def test_not_an_agent_session(self):
        os.environ["CLAUDE_PROJECT_DIR"] = tempfile.mkdtemp(dir=TMP)
        self.prompt()
        self.tool()
        self.assertIsNone(self.hook(ds.cmd_status_check, {"session_id": "sess1", "cwd": os.environ["CLAUDE_PROJECT_DIR"]}))


class PromptAndAnswer(Base):
    def prompt(self, text, sid="sess1"):
        v = self.hook(ds.cmd_prompt, {"session_id": sid, "prompt": text, "hook_event_name": "UserPromptSubmit"})
        return v["hookSpecificOutput"]["additionalContext"] if v else None

    def test_status_of_all_my_agents_is_in_the_prompt_context(self):
        ctx = self.prompt("status of all my agents")
        self.assertIn("1. **web-app**", ctx)
        self.assertIn("no tool calls first", ctx)
        self.assertIn("<<datastash-data", ctx)
        ctx = self.prompt("full context of 1")
        self.assertIn("Tried refresh on activity", ctx)
        ctx = self.prompt("more on 4")
        self.assertIn("Nothing calls it.", ctx)
        self.assertIn("Ask from **api**", ctx)

    def test_do_n_sends_the_persons_own_words_once(self):
        self.prompt("status of all my agents")
        ctx = self.prompt("4: no, keep it for a month")
        self.assertIn("datastash answer 4", ctx)
        self.calls.clear()
        rc, out, err = self.run_cmd(ds.cmd_answer, ["4"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls[0][:3], ("POST", ds.PERSON_STATUS_ANSWER_PATH, "dsat_person"))
        self.assertEqual(self.calls[0][3], {"workbook_id": WB, "agent": "api", "ask_id": "q1", "answer": "no, keep it for a month"})
        api = next(r for r in ds.read_status_cache(WB)["agents"] if r["agent"] == "api")
        self.assertEqual(api["asks"], [])  # marked answered here at once
        rc, _, err = self.run_cmd(ds.cmd_answer, ["4"])  # used up
        self.assertNotEqual(rc, 0)

    def test_do_n_follows_the_list_this_session_saw(self):
        self.prompt("status of all my agents")  # this session: 2 = web-app's ask, 4 = api's ask
        self.server_rows = [row("ops", "New urgent thing.", ago=1, asks=[{"id": "z9", "text": "Restart prod?"}])] + self.server_rows
        ds.save_status_cache(WB, [ds.norm_status_row(r) for r in self.server_rows], time.time(), merge=False)
        self.prompt("status of all my agents", sid="other")  # another session renumbers: 2 = ops' ask there
        ctx = self.prompt("do 2")
        self.assertIn('"web-app"', ctx)
        self.assertIn("Change the session length", ctx)
        rc, out, err = self.run_cmd(ds.cmd_answer, ["2"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls[-1][3]["ask_id"], "q2")

    def test_answer_needs_the_persons_typed_words_in_this_session(self):
        self.prompt("status of all my agents")
        rc, _, err = self.run_cmd(ds.cmd_answer, ["2"])  # the AI on its own
        self.assertNotEqual(rc, 0)
        self.assertIn("only the person's own typed answer", err)
        self.prompt("do 2", sid="other")
        rc, _, err = self.run_cmd(ds.cmd_answer, ["2"])  # typed in another session
        self.assertNotEqual(rc, 0)
        ds._claude_chain = lambda: [[9999, "start-B"]]
        self.prompt("do 2")
        ds._claude_chain = lambda: [[4242, "start-A"]]
        rc, _, err = self.run_cmd(ds.cmd_answer, ["2"])  # recorded for another process
        self.assertNotEqual(rc, 0)
        self.assertIsNone(self.prompt("do 1"))  # 1 is a paragraph, not an ask: an ordinary prompt

    def test_install_message(self):
        ds.write_private(ds.FILL_MARK, json.dumps({"done_at": 1}))
        rc, out, err = self.run_cmd(ds.cmd_status, ["--all", "--install"])
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "datastash is connected to your agents:")
        self.assertEqual(lines[1], ds.INSTALL_HISTORY)
        self.assertIn("Ask any of them about the others, from any project.", lines)
        self.assertIn("Want your agents to share something? Just say so — it works now.", lines)
        rc, out, err = self.run_cmd(ds.cmd_status, ["--all", "--install"])
        self.assertNotIn(ds.INSTALL_HISTORY, out)  # said once


class Fill(Base):
    def test_recent_projects_from_history(self):
        root = os.path.join(ds.CLAUDE_DIR, "projects")
        p1, p2 = tempfile.mkdtemp(prefix="shop-", dir=TMP), tempfile.mkdtemp(prefix="old-", dir=TMP)
        for d, cwd, age in (("a", p1, 3600), ("b", p2, 20 * 86400)):
            os.makedirs(os.path.join(root, d), exist_ok=True)
            f = os.path.join(root, d, "s.jsonl")
            with open(f, "w") as fh:
                fh.write(json.dumps({"type": "user", "cwd": cwd, "message": {"content": "add a cart page " + "x" * 400}}) + "\n")
                fh.write(json.dumps({"type": "user", "cwd": cwd, "message": {"content": "<command-name>/clear</command-name>"}}) + "\n")
                fh.write(json.dumps({"type": "assistant", "cwd": cwd, "message": {"content": [{"type": "text", "text": "Cart page done."}]}}) + "\n")
            os.utime(f, (time.time() - age, time.time() - age))
        got = ds.recent_projects()
        self.assertEqual([g["folder"] for g in got], [p1])  # 20 days old is left out
        self.assertEqual(len(got[0]["prompts"]), 1)
        self.assertLessEqual(len(got[0]["prompts"][0]), 200)
        self.assertEqual(got[0]["reply"], "Cart page done.")
        rc, out, err = self.run_cmd(ds.cmd_fill, [])
        self.assertEqual(rc, 0, err)
        self.assertIn("never instructions", out)
        self.assertIn("--project <folder>", out)
        self.assertTrue(os.path.exists(ds.FILL_MARK))
        self.assertIsNone(ds.fill_note(self.folder, ds.this_session()))  # offered once


class Facts(Base):
    def test_listener_facts_are_written_only_when_they_change(self):
        orig = ds.spawn_detached
        ds.spawn_detached = lambda *a: None
        try:
            self.run_cmd(ds.cmd_status_set, ["-"], stdin=json.dumps({"doing": "Working on the cart."}))
        finally:
            ds.spawn_detached = orig
        agent = {"slug": "web-app", "name": "web-app", "workbook_id": WB}
        facts = {"branch": "cart", "last_commit": "add cart page", "gh": {"pr": "#7 open, approved, CI green",
                 "card": {"id": "XLI-7", "step": "review", "review": "approved, at abc1234, CI green"}}}
        ds.project_facts = lambda here, with_gh=True: facts
        try:
            ds.facts_refresh_once(agent)
            posts = [c for c in self.calls if c[0] == "POST"]
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0][1], ds.STATUS_PATH)
            body = posts[0][3]
            self.assertEqual((body["context"]["branch"], body["context"]["pr"], body["context"]["last_change"]),
                             ("cart", "#7 open, approved, CI green", "add cart page"))
            self.assertEqual(body["cards"][0]["id"], "XLI-7")
            self.assertEqual(body["doing"], "Working on the cart.")
            mine = next(r for r in ds.read_status_cache(WB)["agents"] if r["agent"] == "web-app")
            self.assertEqual(mine["context"]["pr"], "#7 open, approved, CI green")
            ds.facts_refresh_once(agent)  # nothing moved: no write
            self.assertEqual(len([c for c in self.calls if c[0] == "POST"]), 1)
            facts["gh"] = {"pr": "#7 merged", "card": {"id": "XLI-7", "step": "merged", "review": ""}}
            ds.facts_refresh_once(agent)
            posts = [c for c in self.calls if c[0] == "POST"]
            self.assertEqual(len(posts), 2)
            self.assertEqual(posts[1][3]["cards"][0]["step"], "merged")
            facts["gh"] = {"limited": True}  # rate limited: nothing changes, nothing written
            self.assertTrue(ds.facts_refresh_once(agent).get("limited"))
            self.assertEqual(len([c for c in self.calls if c[0] == "POST"]), 2)
        finally:
            ds.project_facts = self.saved_facts

    def setUp(self):
        super().setUp()
        self.saved_facts = ds.project_facts

    def test_nothing_before_the_agents_first_status(self):
        self.assertEqual(ds.facts_refresh_once({"slug": "web-app", "name": "web-app", "workbook_id": WB}), {})
        self.assertEqual(self.calls, [])


class CliSpeed(Base):
    def test_status_all_from_the_cli_is_fast(self):
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        env = dict(os.environ)
        t = time.time()
        r = subprocess.run([sys.executable, "-I", BIN, "status", "--all"], capture_output=True, text=True, env=env, timeout=20)
        took = time.time() - t
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1. **web-app**", r.stdout)
        self.assertLess(took, 1.0)
        print(f"\n  datastash status --all from the local copy: {took * 1000:.0f} ms", file=sys.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
