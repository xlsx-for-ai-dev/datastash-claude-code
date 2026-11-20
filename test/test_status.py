#!/usr/bin/env python3
"""Offline tests for the agent status (owner 11-20, v2 after EPM's review): the numbered list (bounded, grouped by
agent and project, every value with its age), the local copy, the end-of-turn reminder (only on a material change,
never while the door is missing, at most every 10 minutes), the background write (only after the person's yes), "do N"
answers (only against the list this session showed, only from the real prompt hook, sent by the hook), "full context
of N", and the first setup (one yes before anything goes up). No network, no key.
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
import uuid
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "..", "plugins", "datastash", "scripts", "datastash")
TMP = tempfile.mkdtemp(prefix="ds-status-test-")
os.environ["DATASTASH_CONFIG_DIR"] = TMP
os.environ["DATASTASH_MAIN_DIR"] = os.path.join(TMP, "home", "datastash main")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "claude-config")
os.environ["DATASTASH_STATUS_RETRIES"] = "0.01,0.01"
os.environ["DATASTASH_VERIFY_WAIT"] = "0.3"
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
    row("docs", "Nothing in flight since Monday. Waiting for the next card.", ago=3 * 86400, project="docs"),
    row("api", "Billing refactor done; cleaning up two leftover TODOs.", ago=3600, project="api",
        context={"branch": "main", "pr": "no PR", "tests": "all pass", "last_change": "moved invoices to the new table"},
        asks=[{"id": "q1", "version": 3, "text": "Delete the old /invoices endpoint?", "touches": "src/routes/invoices.ts",
               "if_yes": "removes 140 lines", "if_no": "I leave it with a deprecation note", "details": "Nothing calls it."}]),
    row("web-app", "Fixing the login timeout; sessions expire after a day, so users get kicked out mid-task. Two tests still fail.",
        ago=300, project="web-app", details="Tried refresh on activity; the two failing tests expect 7 days.",
        context={"branch": "fix-login", "pr": "#41 open", "tests": "38 pass, 2 fail", "last_change": "session refresh on activity"},
        asks=[{"id": "q2", "version": 1, "text": "Change the session length from 1 day to 7 days?", "touches": "src/auth/session.ts",
               "if_yes": "fixes the 2 failing tests", "if_no": "I add a refresh prompt instead"}],
        cards=[{"id": "XLI-12", "step": "review", "review": "Opus FAIL at abc1234, CI red", "blocker": "a failing test"}],
        running=[{"what": "builder", "on": "XLI-12", "stuck": True}],
        waiting_on=[{"what": "the EPM pass", "who": "EPM"}],
        done_asked=[{"request": "post the summary", "done": True, "proof": "posted to #team"}]),
]


def framed_inner(text):
    """The lines between the data frame's start and end markers."""
    lines = text.splitlines()
    i = next(k for k, x in enumerate(lines) if x.startswith("<<datastash-data "))
    j = next(k for k, x in enumerate(lines) if x.startswith("<<end datastash-data "))
    return lines[i + 1:j]


class Base(unittest.TestCase):
    def setUp(self):
        ds.ensure_dirs()
        for n in os.listdir(ds.STATE):
            os.replace(os.path.join(ds.STATE, n), os.path.join(TMP, f"old-{n}-{time.time()}"))
        for n in os.listdir(ds.CONF):
            if n.startswith("status-") or n.startswith("agent-"):
                os.replace(os.path.join(ds.CONF, n), os.path.join(TMP, f"old-{n}-{time.time()}"))
        self.saved = {k: getattr(ds, k) for k in ("http", "read_key", "hook_input", "_claude_chain", "signed_in",
                                                    "login_token", "gh_pr", "this_session", "spawn_detached",
                                                    "project_facts")}
        self.calls = []
        self.spawned = []
        self.server_rows = [dict(r) for r in EXAMPLE]
        self.fail_with = None
        self.answer_error = None

        def fake_http(method, path, key=None, body=None, form=None, timeout=40):
            self.calls.append((method, path, key, body))
            if self.fail_with is not None:
                raise self.fail_with
            if method == "POST" and path == ds.PERSON_STATUS_ANSWER_PATH and self.answer_error is not None:
                raise self.answer_error
            if method == "GET" and path.startswith((ds.STATUS_PATH + "?", ds.PERSON_STATUS_PATH + "?")):
                return 200, "application/json", json.dumps({"workbook_id": WB, "agents": [dict(r) for r in self.server_rows]})
            return 200, "application/json", json.dumps({"ok": True})
        ds.http = fake_http
        ds.read_key = lambda s: "dsat_person" if ds.person_slug(s) else "ldgr_ag_test"
        ds.signed_in = lambda: True
        ds.login_token = lambda force=False: "dsat_person"
        ds.gh_pr = lambda here: {}
        ds._claude_chain = lambda: [[4242, "start-A"]]
        ds.spawn_detached = lambda argv, cwd: self.spawned.append(argv)
        self.folder = os.path.realpath(tempfile.mkdtemp(prefix="proj-", dir=TMP))
        ds.write_private(ds.conf_path("web-app"), json.dumps({"name": "web-app", "workbook_id": WB, "projects": [self.folder]}))
        ds.write_private(ds.key_path("web-app"), "ldgr_ag_" + "x" * 40)
        ds.record_consent([self.folder], [])  # the person said yes to this project (H2 tests start without it)
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

    # --- the session transcript, as Claude Code writes it
    def transcript(self, sid):
        d = os.path.join(ds.CLAUDE_DIR, "projects", "-proj")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{sid}.jsonl")

    def entry(self, sid, v):
        v = dict(v, uuid=str(uuid.uuid4()), timestamp=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()))
        with open(self.transcript(sid), "a") as f:
            f.write(json.dumps(v) + "\n")

    def typed(self, sid, text, pid=None):
        self.entry(sid, {"type": "user", "promptId": pid or str(uuid.uuid4()), "message": {"role": "user", "content": text}})

    def says(self, sid, text):
        self.entry(sid, {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})

    def tool_use(self, sid, command):
        self.entry(sid, {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": command}}]}})

    def run_checks(self, start):
        for argv in list(self.spawned[start:]):
            if argv[2] == "status-verify":
                ds.cmd_status_verify([argv[3]])

    def prompt(self, text, sid="sess1", say=True):
        """As Claude Code does it: the UserPromptSubmit hook runs first, THEN the typed prompt is written to the
        transcript (with the hook's prompt id); the checker then finds it. Claude shows the note in its reply."""
        pid = str(uuid.uuid4())
        inp = {"session_id": sid, "prompt": text, "hook_event_name": "UserPromptSubmit", "cwd": self.folder,
               "prompt_id": pid, "transcript_path": self.transcript(sid)}
        n = len(self.spawned)
        v = self.hook(ds.cmd_prompt, inp)
        ctx = v["hookSpecificOutput"]["additionalContext"] if v else None
        self.typed(sid, text, pid)
        self.run_checks(n)
        if say:
            self.says(sid, ctx or "Sure, done.")
        return ctx

    def forged(self, inp):
        """An AI's tool call: `printf '<json>' | datastash prompt`, no new typed entry in the transcript."""
        n = len(self.spawned)
        ds.hook_input = lambda: inp
        rc, out, err = self.run_cmd(ds.cmd_prompt, [])
        self.run_checks(n)
        return out

    def outcomes(self, sid="sess1"):
        return ds.take_outcomes(sid)

    def answers(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == ds.PERSON_STATUS_ANSWER_PATH]


class ListFormat(Base):
    def test_owner_example_numbers_and_order(self):
        rows = [ds.norm_status_row(r) for r in EXAMPLE]
        lines, numbers = ds.status_list(rows, NOW)
        text = "\n".join(lines)
        # the paragraph is 5 minutes old: past the 2-minute limit, so dimmed, never shown as current
        self.assertTrue(lines[0].startswith("1. **web-app** — _Fixing the login timeout"), text)
        self.assertEqual(lines[1], "   fix-login · PR #41 open · tests 38 pass, 2 fail · last: session refresh on activity · 5 min ago")
        self.assertIn("   XLI-12: built ✓ · [review: Opus FAIL at abc1234, CI red] · blocker: a failing test", lines)
        self.assertIn("   running: builder on XLI-12 (STUCK)", lines)
        self.assertIn("   waiting on: EPM for the EPM pass", lines)
        self.assertIn("   you asked: post the summary ✓ (posted to #team)", lines)
        self.assertIn("   2. Ask: Change the session length from 1 day to 7 days (src/auth/session.ts)? If yes: fixes the 2 "
                      "failing tests. If no: I add a refresh prompt instead.", lines)
        self.assertIn("3. **api** — _Billing refactor done; cleaning up two leftover TODOs._", lines)
        # GitHub facts an hour old: dimmed with their age
        self.assertIn("   main · _no PR (1 h ago)_ · tests all pass · last: moved invoices to the new table · 1 h ago", lines)
        self.assertTrue(lines[-1].startswith("5. **docs** — _Nothing in flight since Monday._ · 3 d ago"), text)
        self.assertEqual(numbers["2"], {"agent": "web-app", "project": "web-app", "ask": "q2", "ask_version": 1,
                                        "text": "Change the session length from 1 day to 7 days?"})
        self.assertEqual((numbers["4"]["agent"], numbers["4"]["ask"], numbers["4"]["ask_version"]), ("api", "q1", 3))
        self.assertEqual(numbers["5"], {"agent": "docs", "project": "docs", "ask": None})

    def test_fresh_values_are_not_dimmed_and_unknown_is_blank(self):
        r = ds.norm_status_row(row("web-app", "Writing the tests now.", ago=30, project="web-app",
                                   context={"branch": "b", "pr": "#9 open", "as_of": {"gh": NOW - 30}}, asks=["Ship it?"]))
        lines, _ = ds.status_list([r], NOW)
        self.assertEqual(lines[0], "1. **web-app** — Writing the tests now.")
        self.assertEqual(lines[1], "   b · PR #9 open · just now")  # no tests, no last change: left out, never guessed
        r = ds.norm_status_row(row("web-app", "Same.", ago=30, project="web-app",
                                   context={"pr": "#9 open", "as_of": {"gh": NOW - 600}}, asks=["Ship it?"]))
        self.assertIn("_PR #9 open (10 min ago)_", ds.status_list([r], NOW)[0][1])

    def test_grouped_by_agent_with_a_roll_up(self):
        rows = [ds.norm_status_row(row("api", "Billing work.", ago=30, project="billing", asks=[{"id": "b1", "text": "Merge?"}])),
                ds.norm_status_row(row("api", "Invoice cleanup.", ago=30, project="invoices", cards=[{"id": "XLI-3", "step": "built"}]))]
        lines, numbers = ds.status_list(rows, NOW)
        self.assertEqual(lines[0], "**api** — on 2 projects: billing, invoices")
        self.assertEqual(lines[1], "1. **api** · billing — Billing work.")
        self.assertEqual(numbers["2"]["project"], "billing")
        self.assertIn("3. **api** · invoices — Invoice cleanup.", lines)

    def test_agent_text_is_one_line_and_answered_asks_drop(self):
        r = ds.norm_status_row(row("x", "line one\nline two\x1b[31m", asks=[{"text": "ok?", "answered": True}, "keep?"]))
        self.assertEqual(r["doing"], "line one line two [31m")
        self.assertEqual(ds._clean("a‮b c"), "a b c")  # no direction flips or hidden line breaks
        self.assertEqual([a["text"] for a in r["asks"]], ["keep?"])
        self.assertEqual(r["asks"][0]["id"], ds.ask_id("keep?"))  # stable id from the words
        self.assertIsNone(ds.norm_status_row({"doing": "no name"}))


class Bounded(Base):
    """M1: one screen is bounded (a layout, not a data limit): asks first, 10 items per agent, 30 agents, ~8 KB."""
    def test_50_rows_x_200_asks_stays_within_the_bound(self):
        big = [row(f"agent-{i:02d}", "Working through a long queue of decisions. " * 5, ago=30, project=f"p{i}",
                   asks=[{"id": f"a{i}-{k}", "text": f"Approve change {k} of {i}? " + "x" * 250, "touches": "y" * 150,
                          "if_yes": "z" * 230, "if_no": "w" * 230, "details": "d" * 1900} for k in range(200)])
               for i in range(50)]
        ds.save_status_cache(WB, [ds.norm_status_row(r) for r in big], time.time(), merge=False)
        out = "\n".join(ds.render_status_all(self.folder, ds.lazy_me()))
        self.assertLessEqual(len(out), 8500, len(out))
        self.assertIn("more: say 'more on agent-", out)
        self.assertRegex(out, r"\d+ more agents \(.*\): say 'more on <agent>'")
        self.assertLessEqual(out.count(". Ask: "), 10 * 30)
        # nothing dropped from storage
        c = ds.read_status_cache(WB)
        self.assertEqual(len(c["agents"]), 50)
        self.assertEqual(sum(len(r["asks"]) for r in c["agents"]), 50 * 200)
        # "more on <agent>" shows that agent's next asks, still bounded
        more = "\n".join(ds.render_status_all(self.folder, ds.lazy_me(), only="agent-07"))
        self.assertLessEqual(len(more), 9000)
        self.assertIn("**agent-07**", more)
        self.assertNotIn("agent-08", more)

    def test_per_agent_cap(self):
        r = row("api", "Busy.", ago=30, project="api", asks=[{"id": f"q{k}", "text": f"Q{k}?"} for k in range(25)])
        lines, numbers = ds.status_list([ds.norm_status_row(r)], NOW)
        self.assertEqual(len([v for v in numbers.values() if v["ask"]]), 10)
        self.assertIn("   15 more: say 'more on api'", lines)


class Phrases(unittest.TestCase):
    def test_phrases(self):
        P = ds.status_phrase
        self.assertEqual(P("status of all my agents"), ("all", None))
        self.assertEqual(P("What's the status of all my agents?"), ("all", None))
        self.assertEqual(P("what are my agents doing"), ("all", None))
        self.assertEqual(P("full context of 4"), ("details", 4))
        self.assertEqual(P("more on 12"), ("details", 12))
        self.assertEqual(P("more on api"), ("more", "api"))
        self.assertEqual(P("details 3"), ("details", 3))
        self.assertEqual(P("do 4"), ("answer", (4, "yes")))
        self.assertEqual(P("4: no, 30 days"), ("answer", (4, "no, 30 days")))
        self.assertIsNone(P("2) update the readme\n3) bump the version"))
        for t in ("2-3 more tests please", "2:30 works for me", "5-star ratings only", "1) fix the login bug",
                  "10 - 15 minutes is fine"):
            self.assertIsNone(P(t), t)
        for t in ("fix the agent status bug in api", "do 4 things for me please", "the status of my agents table is wrong "
                  "and needs a fix", "4", ""):
            self.assertIsNone(P(t), t)

    def test_fill_replies(self):
        fill = {"1": {"folder": "/a/shop", "name": "shop"}, "2": {"folder": "/a/api", "name": "api"}}
        F = ds.fill_reply
        self.assertEqual(F("yes", fill), ("keep", ["/a/shop", "/a/api"]))
        self.assertEqual(F("Yes, keep all.", fill), ("keep", ["/a/shop", "/a/api"]))
        self.assertEqual(F("no thanks", fill), ("keep", []))
        self.assertEqual(F("keep 2", fill), ("keep", ["/a/api"]))
        self.assertEqual(F("just shop", fill), ("keep", ["/a/shop"]))
        self.assertEqual(F("shop and api", fill), ("keep", ["/a/shop", "/a/api"]))
        for t in ("yes but first fix the build", "keep 3", "what does this send?", "do 2"):
            self.assertIsNone(F(t, fill), t)


class LocalCopy(Base):
    def test_fresh_copy_needs_no_network_and_is_fast(self):
        ds.fetch_status({"slug": "web-app", "workbook_id": WB, "name": "web-app"})
        self.calls.clear()
        t = time.time()
        out = ds.render_status_all(self.folder, ds.this_session())
        self.assertLess(time.time() - t, 0.1)
        self.assertEqual(self.calls, [])
        self.assertIn("(refreshed just now)", out[-1])
        self.assertEqual(os.stat(ds.status_cache_path(WB)).st_mode & 0o777, 0o600)

    def test_old_copy_is_fetched_once_and_a_failure_shows_the_old_copy_honestly(self):
        ds.save_status_cache(WB, [ds.norm_status_row(EXAMPLE[0])], time.time() - 600, merge=False)
        out = ds.render_status_all(self.folder, ds.this_session())
        self.assertEqual(len([c for c in self.calls if c[0] == "GET"]), 1)
        self.assertEqual(self.calls[0][2], "ldgr_ag_test")  # an agent session reads with its own key
        self.assertIn("**web-app**", "\n".join(out))
        ds.save_status_cache(WB, [ds.norm_status_row(EXAMPLE[0])], time.time() - 600, merge=False)
        self.fail_with = ds.ApiError(503, {"code": "busy"})
        out = ds.render_status_all(self.folder, ds.this_session())
        self.assertIn("refreshed 10 min ago; the last refresh failed just now", out[-1])  # never fake freshness
        self.assertIn("showing the copy on this machine", out[-1])
        self.assertIn("**docs**", "\n".join(out))

    def test_door_missing_answers_from_the_copy_at_once(self):
        """L3: while the status door is 404, no live call: the copy at once, with its age."""
        ds.save_status_cache(WB, [ds.norm_status_row(EXAMPLE[0])], time.time() - 600, merge=False)
        self.fail_with = ds.ApiError(404, {"error": "not_found"})
        ds.render_status_all(self.folder, ds.this_session())  # the 404 is noted
        self.assertTrue(ds.door_missing())
        self.calls.clear()
        t = time.time()
        out = ds.render_status_all(self.folder, ds.this_session())
        self.assertLess(time.time() - t, 0.2)
        self.assertEqual(self.calls, [])
        self.assertIn("datastash has no agent status service yet; showing the copy on this machine", out[-1])
        self.assertIn("refreshed 10 min ago", out[-1])
        self.fail_with = None
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})  # the door appears: cleared
        self.assertFalse(ds.door_missing())

    def test_refresher_refreshes_on_wake_and_backs_off_on_404(self):
        wake, stop = threading.Event(), threading.Event()
        ds.STATUS_REFRESH_SECONDS, old = 30, ds.STATUS_REFRESH_SECONDS
        try:
            th = threading.Thread(target=ds.status_refresher, args=({"slug": "web-app", "workbook_id": WB}, wake, stop), daemon=True)
            th.start()
            time.sleep(0.2)
            self.assertEqual(len(self.calls), 1)  # at start
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
        self.spawned.clear()
        rc, out, err = self.run_cmd(ds.cmd_status_set, list(args), stdin=json.dumps(data) if data is not None else "")
        return rc, out, err, list(self.spawned)

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
        self.assertEqual(mine["project"], os.path.basename(self.folder))
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
        self.assertEqual((body["workbook_id"], body["project"]), (WB, os.path.basename(self.folder)))  # keyed (agent, project)
        self.assertEqual(body["asks"][0]["text"], "Merge it?")
        self.assertEqual(set(body), {"workbook_id", "project", "doing", "details", "context", "asks", "cards", "running",
                                     "health", "done_asked", "waiting_on"})
        self.assertEqual(ds.read_json(ds._sent_path("web-app"), {}).get("seq"), seq)

    def test_the_agents_words_go_up_whole(self):
        """No new size caps: only the screen is cut; what is sent is whole."""
        long = "Plain words about the work. " * 100
        _, _, _, started = self.status_set({"doing": long, "details": long, "asks": [{"text": "Ship it? " + long}]})
        ds.cmd_status_send(["web-app", started[0][4]])
        body = [c for c in self.calls if c[0] == "POST"][0][3]
        self.assertEqual(body["doing"], long.strip())
        self.assertEqual(body["details"], long.strip())
        self.assertEqual(body["asks"][0]["text"], ("Ship it? " + long).strip())
        mine = next(r for r in ds.read_status_cache(WB)["agents"] if r["agent"] == "web-app")
        self.assertEqual(mine["asks"][0]["id"], body["asks"][0]["id"])  # one id for the ask
        self.assertLessEqual(len(mine["doing"]), 600)  # the screen copy is cut for showing

    def test_no_send_while_the_door_is_missing(self):
        ds.note_door(False)
        _, _, _, started = self.status_set({"doing": "Door not there yet."})
        ds.cmd_status_send(["web-app", started[0][4]])
        self.assertEqual([c for c in self.calls if c[0] == "POST"], [])

    def test_offline_write_is_sent_again_by_the_listener(self):
        _, _, _, s1 = self.status_set({"doing": "Offline turn."})
        self.fail_with = OSError("down")
        ds.cmd_status_send(["web-app", s1[0][4]])  # retries run out
        self.assertNotEqual(ds.read_json(ds._sent_path("web-app"), {}).get("seq"), s1[0][4])
        self.spawned.clear()
        self.assertTrue(ds.resend_unsent("web-app"))  # after a good refresh: sent again, same seq
        self.assertEqual(self.spawned[0][2:5], ["status-send", "web-app", s1[0][4]])
        self.assertFalse(ds.resend_unsent("web-app"))  # not again within 5 minutes

    def test_second_turn_changes_the_stored_status_and_an_old_sender_steps_aside(self):
        _, _, _, s1 = self.status_set({"doing": "Turn one."})
        _, _, _, s2 = self.status_set({"doing": "Turn two: all tests pass now."})
        mine = next(r for r in ds.read_status_cache(WB)["agents"] if r["agent"] == "web-app")
        self.assertEqual(mine["doing"], "Turn two: all tests pass now.")
        self.assertEqual(ds.cmd_status_send(["web-app", s1[0][4]]), 0)
        self.assertEqual([c for c in self.calls if c[0] == "POST"], [])
        ds.cmd_status_send(["web-app", s2[0][4]])
        self.assertEqual([c[3]["doing"] for c in self.calls if c[0] == "POST"], ["Turn two: all tests pass now."])
        self.server_rows = [row("web-app", "Turn one.", ago=999, project=os.path.basename(self.folder))]
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

    def test_project_flag_only_for_this_folders_agent(self):
        """L2: --project writes only for the agent linked to the current folder."""
        other = os.path.realpath(tempfile.mkdtemp(prefix="other-", dir=TMP))
        ds.write_private(ds.conf_path("billing"), json.dumps({"name": "billing", "workbook_id": WB, "projects": [other]}))
        ds.write_private(ds.key_path("billing"), "ldgr_ag_" + "y" * 40)
        ds.record_consent([other], [])
        rc, _, err, started = self.status_set({"doing": "Overwrite billing."}, args=("-", "--project", other))
        self.assertNotEqual(rc, 0)
        self.assertIn("--project works only for this folder's own agent", err)
        self.assertEqual(started, [])
        self.assertIsNone(ds.read_json(ds._outbox_path("billing"), None))
        second = os.path.realpath(tempfile.mkdtemp(prefix="second-", dir=TMP))  # web-app's own second folder: fine
        ds.write_private(ds.conf_path("web-app"), json.dumps({"name": "web-app", "workbook_id": WB, "projects": [self.folder, second]}))
        ds.record_consent([second], [])
        rc, out, err, started = self.status_set({"doing": "The second folder."}, args=("-", "--project", second))
        self.assertEqual(rc, 0, err)
        self.assertIn(f"({os.path.basename(second)})", out)


class StopHook(Base):
    def stop(self, active=False, **kw):
        return self.hook(ds.cmd_status_check, dict({"session_id": "sess1", "cwd": self.folder, "stop_hook_active": active}, **kw))

    def turn(self):
        ds.turn_started("sess1")
        time.sleep(0.01)

    def tool(self, command="ls"):
        ds.hook_input = lambda: {"session_id": "sess1", "hook_event_name": "PostToolUse", "tool_name": "Bash",
                                 "tool_input": {"command": command}}
        self.run_cmd(ds.cmd_agent_tool, [])

    def write(self, doing="Done for now."):
        self.run_cmd(ds.cmd_status_set, ["-"], stdin=json.dumps({"doing": doing}))

    def ten_minutes_pass(self):
        p = os.path.join(ds.STATE, "status-reminded-web-app")
        if os.path.exists(p):
            os.utime(p, (time.time() - 700, time.time() - 700))

    def test_asks_once_and_never_loops(self):
        self.turn()
        self.assertIsNone(self.stop())  # no tool use: a trivial turn
        self.tool()
        v = self.stop()  # no status yet from this machine: worth one
        self.assertEqual(v["decision"], "block")
        self.assertIn("datastash status-set -", v["reason"])
        self.assertIsNone(self.stop(active=True))
        self.assertIsNone(self.stop())  # at most once per turn
        self.turn()
        self.tool()
        self.assertIsNone(self.stop())  # and at most once every 10 minutes per agent
        self.ten_minutes_pass()
        self.assertEqual(self.stop()["decision"], "block")

    def test_quiet_without_a_material_change(self):
        self.turn()
        self.tool()
        self.write()
        self.ten_minutes_pass()
        self.turn()
        self.tool("ls")
        self.assertIsNone(self.stop())  # nothing changed since the last status: no model turn spent
        self.tool("python3 -m pytest -q")  # a test run is a material change
        self.assertEqual(self.stop()["decision"], "block")

    def test_a_changed_file_or_a_new_ask_is_a_change(self):
        subprocess.run(["git", "init", "-q", self.folder], check=True)
        self.turn()
        self.tool()
        self.write()
        self.ten_minutes_pass()
        self.turn()
        self.tool()
        self.assertIsNone(self.stop())
        with open(os.path.join(self.folder, "new.txt"), "w") as f:
            f.write("x")
        self.assertEqual(self.stop()["decision"], "block")
        self.write()
        self.ten_minutes_pass()
        self.turn()
        self.tool()
        self.assertIsNone(self.stop(last_assistant_message="All done."))
        self.assertEqual(self.stop(last_assistant_message="Should I delete the old endpoint?")["decision"], "block")

    def test_never_while_the_door_is_missing(self):
        ds.note_door(False)
        self.turn()
        self.tool()
        self.assertIsNone(self.stop())
        ds.note_door(True)
        self.assertEqual(self.stop()["decision"], "block")

    def test_never_for_a_project_without_the_persons_yes(self):
        ds.write_private(ds.CONSENT_PATH, json.dumps({"keep": {}, "no": {}}))
        self.turn()
        self.tool()
        self.assertIsNone(self.stop())

    def test_a_status_written_this_turn_ends_quietly(self):
        self.turn()
        self.tool()
        self.write()
        self.assertIsNone(self.stop())

    def test_not_an_agent_session(self):
        os.environ["CLAUDE_PROJECT_DIR"] = tempfile.mkdtemp(dir=TMP)
        self.turn()
        self.tool()
        self.assertIsNone(self.hook(ds.cmd_status_check, {"session_id": "sess1", "cwd": os.environ["CLAUDE_PROJECT_DIR"]}))


class PromptAndAnswer(Base):
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

    def test_h1_session_that_never_showed_a_list_sends_nothing(self):
        """EPM's probe: session A shows the list; session B never did; "2: yes, go with option B" in B is ordinary."""
        self.prompt("status of all my agents", sid="sessA")
        self.calls.clear()
        self.assertIsNone(self.prompt("2: yes, go with option B", sid="sessB"))
        self.assertIsNone(self.prompt("do 2", sid="sessB"))
        self.assertEqual(self.answers(), [])
        self.assertFalse(any(n.startswith(("status-pending", "status-answer")) for n in os.listdir(ds.STATE)))

    def test_h1_claudes_own_numbered_options_after_the_list_are_not_answers(self):
        self.prompt("status of all my agents")
        self.prompt("thanks; now plan the login fix", say=False)
        self.says("sess1", "Two ways to go:\n1. Refresh tokens\n2. Longer sessions\nWhich one?")
        self.calls.clear()
        self.assertIsNone(self.prompt("2: yes, go with option B"))  # inside 3 prompts, but not the last thing shown
        self.assertEqual(self.answers(), [])

    def test_h1_list_expires_after_3_prompts_or_30_minutes(self):
        self.prompt("status of all my agents")
        for t in ("fix the readme", "now the tests", "and the changelog"):
            self.prompt(t, say=False)
        self.calls.clear()
        self.assertIsNone(self.prompt("2: yes, go with option B"))  # 4 prompts later
        self.prompt("status of all my agents")
        view = ds.read_json(ds._view_path("sess1"), {})
        ds.write_private(ds._view_path("sess1"), json.dumps(dict(view, at=time.time() - 1801)))
        self.assertIsNone(self.prompt("do 2"))  # 31 minutes later
        self.assertEqual(self.answers(), [])

    def test_h1_a_newer_list_replaces_the_older(self):
        self.prompt("status of all my agents")
        self.server_rows = [row("ops", "Urgent.", ago=1, project="ops", asks=[{"id": "z9", "version": 1, "text": "Restart prod?"}])]
        ds.save_status_cache(WB, [ds.norm_status_row(r) for r in self.server_rows], time.time(), merge=False)
        self.prompt("more on ops")  # the most recent list: 2 = ops' ask now
        self.prompt("do 2")
        self.assertEqual(self.answers()[-1][3]["ask_id"], "z9")

    def test_answer_is_sent_by_the_plugin_with_version_and_idempotency_key(self):
        self.prompt("status of all my agents")
        self.calls.clear()
        ctx = self.prompt("4: no, keep it for a month", say=False)
        self.says("sess1", framed_inner(ctx)[0])  # Claude shows the one line, as the note says
        self.assertNotIn("datastash answer", ctx)  # the AI is never told to run anything
        inner = framed_inner(ctx)  # L1: the agent's ask text is inside the data frame
        self.assertEqual(inner, ['Sending your answer to api: "Delete the old /invoices endpoint?" → no, keep it for a month'])
        a = self.answers()
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0][2], "dsat_person")
        body = dict(a[0][3])
        self.assertRegex(body.pop("answer_id"), r"^[0-9a-f]{24}$")
        self.assertEqual(body, {"workbook_id": WB, "agent": "api", "project": "api", "ask_id": "q1", "ask_version": 3,
                                "answer": "no, keep it for a month", "relayed": True})
        # the outcome: one line for the person at the end of the turn, with no model turn
        v = self.hook(ds.cmd_status_check, {"session_id": "sess1", "cwd": self.folder})
        self.assertEqual(v, {"systemMessage": 'datastash: Answered api: "Delete the old /invoices endpoint?" → no, keep it for a month'})
        api = next(r for r in ds.read_status_cache(WB)["agents"] if r["agent"] == "api")
        self.assertEqual(api["asks"], [])  # marked answered here at once
        self.prompt("do 2")  # the plugin's own "Sending" line in between does not count as other replies
        self.assertEqual(self.outcomes(), ['Answered web-app: "Change the session length from 1 day to 7 days?" → yes'])

    def test_ask_changed_shows_the_new_text_and_sends_nothing(self):
        self.prompt("status of all my agents")
        rows = [dict(r) for r in EXAMPLE]
        rows[1] = dict(rows[1], asks=[{"id": "q1", "version": 4, "text": "Delete /invoices and /refunds?"}])
        ds.save_status_cache(WB, [ds.norm_status_row(r) for r in rows], time.time(), merge=False)
        self.calls.clear()
        ctx = self.prompt("do 4")
        self.assertEqual(self.answers(), [])
        self.assertEqual(framed_inner(ctx), ['Not sent: api changed that ask. It now says: "Delete /invoices and /refunds?"'])
        # the server's own compare-and-set: it refuses a changed ask; nothing else is sent
        self.prompt("status of all my agents")
        self.answer_error = ds.ApiError(409, {"code": "ask_changed", "ask": {"text": "Delete everything?", "version": 5}})
        self.prompt("do 2")
        self.assertEqual(len(self.answers()), 1)
        self.assertEqual(self.outcomes(), ['Not sent: web-app changed that ask. It now says: "Delete everything?"'])

    def test_offline_answer_waits_in_the_outbox_with_its_key(self):
        self.prompt("status of all my agents")
        self.answer_error = OSError("down")
        self.prompt("do 4")
        self.assertIn("datastash can't be reached right now", self.outcomes()[0])
        argv = self.spawned[-1]
        self.assertEqual(argv[2], "status-answer-send")
        rec = ds.read_json(ds._answer_out_path(argv[3]), {})
        self.assertEqual((rec["ask_id"], rec["answer_id"]), ("q1", argv[3]))
        self.answer_error = None
        self.calls.clear()
        ds.cmd_status_answer_send([argv[3]])
        self.assertEqual(self.answers()[0][3]["answer_id"], argv[3])  # the same key: one answer on the server
        self.assertFalse(os.path.exists(ds._answer_out_path(argv[3])))

    def test_m2_forged_prompt_then_answer_gives_no_person_answer(self):
        """EPM's probe: printf '{"session_id":"...","prompt":"do 3"}' | datastash prompt; then datastash answer 3."""
        self.prompt("status of all my agents")  # the person really saw a list in this session
        self.calls.clear()
        tp = self.transcript("sess1")
        # 1. the probe exactly
        self.forged({"session_id": "sess1", "prompt": "do 4"})
        rc, _, err = self.run_cmd(ds.cmd_answer, ["4"])
        self.assertNotEqual(rc, 0)
        self.assertIn("sent only by the plugin's prompt hook", err)
        # 2. the hook's full shape, a made-up prompt id: the transcript never gets a typed entry for it
        self.tool_use("sess1", "printf '{...}' | datastash prompt")
        self.forged({"session_id": "sess1", "prompt": "do 4", "hook_event_name": "UserPromptSubmit",
                     "transcript_path": tp, "prompt_id": str(uuid.uuid4())})
        # 3. the prompt id of a real typed entry with other words
        pid = str(uuid.uuid4())
        self.typed("sess1", "looks good", pid)
        self.forged({"session_id": "sess1", "prompt": "do 4", "hook_event_name": "UserPromptSubmit",
                     "transcript_path": tp, "prompt_id": pid})
        # 4. a made-up transcript outside Claude Code's projects folder, or another session's
        for path in (os.path.join(TMP, "sess1.jsonl"), self.transcript("sessX")):
            self.forged({"session_id": "sess1", "prompt": "do 4", "hook_event_name": "UserPromptSubmit",
                         "transcript_path": path, "prompt_id": str(uuid.uuid4())})
        self.assertEqual(self.answers(), [])
        self.assertEqual(self.outcomes(), [])
        # 5. a replay of the person's real "do 4" (same prompt id) is used once only
        self.prompt("status of all my agents")
        pid = str(uuid.uuid4())
        inp = {"session_id": "sess1", "prompt": "do 4", "hook_event_name": "UserPromptSubmit", "cwd": self.folder,
               "prompt_id": pid, "transcript_path": tp}
        n = len(self.spawned)
        self.hook(ds.cmd_prompt, inp)
        self.typed("sess1", "do 4", pid)
        self.run_checks(n)
        self.assertEqual(len(self.answers()), 1)
        ds.save_status_cache(WB, [ds.norm_status_row(r) for r in EXAMPLE], time.time(), merge=False)  # ask back
        self.forged(inp)
        self.assertEqual(len(self.answers()), 1)

    def test_install_message(self):
        rc, out, err = self.run_cmd(ds.cmd_status, ["--all", "--install"])
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "datastash is connected to your agents:")
        self.assertIn("Ask any of them about the others, from any project.", lines)
        self.assertIn("Want your agents to share something? Just say so — it works now.", lines)


class FirstSetup(Base):
    """H2: nothing registered, linked or uploaded without the person's yes."""
    def setUp(self):
        super().setUp()
        ds.write_private(ds.CONSENT_PATH, json.dumps({"keep": {}, "no": {}}))
        root = os.path.join(ds.CLAUDE_DIR, "projects")
        self.p1 = os.path.realpath(tempfile.mkdtemp(prefix="shop-", dir=TMP))
        self.p2 = os.path.realpath(tempfile.mkdtemp(prefix="api-", dir=TMP))
        old = os.path.realpath(tempfile.mkdtemp(prefix="old-", dir=TMP))
        for d, cwd, age in (("a", self.p1, 3600), ("b", self.p2, 7200), ("c", old, 20 * 86400)):
            os.makedirs(os.path.join(root, d), exist_ok=True)
            f = os.path.join(root, d, "s.jsonl")
            with open(f, "w") as fh:
                fh.write(json.dumps({"type": "user", "cwd": cwd, "message": {"content": "add a cart page " + "x" * 400}}) + "\n")
                fh.write(json.dumps({"type": "user", "cwd": cwd, "message": {"content": "<command-name>/clear</command-name>"}}) + "\n")
                fh.write(json.dumps({"type": "assistant", "cwd": cwd, "message": {"content": [{"type": "text", "text": "Cart page done."}]}}) + "\n")
            os.utime(f, (time.time() - age, time.time() - age))

    def tearDown(self):
        root = os.path.join(ds.CLAUDE_DIR, "projects")
        for d in ("a", "b", "c"):
            p = os.path.join(root, d)
            if os.path.isdir(p):
                os.replace(p, os.path.join(TMP, f"old-proj-{d}-{time.time()}"))
        super().tearDown()

    def test_no_session_start_instruction(self):
        out = self.hook(ds.cmd_check, {"session_id": "sess9", "cwd": tempfile.mkdtemp(dir=TMP)})
        text = json.dumps(out or {})
        self.assertNotIn("datastash fill", text)
        self.assertNotIn("first setup", text)

    def test_fill_shows_locally_and_sends_nothing(self):
        rc, out, err = self.run_cmd(ds.cmd_fill, [])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.spawned, [])
        self.assertIn(ds.FILL_HEAD, out)
        self.assertIn(ds.FILL_QUESTION, out)
        self.assertIn(ds.FILL_WHAT, out)
        self.assertRegex(out, r"1\. shop-\S+ — .* · last session 1 h ago")
        self.assertNotIn("old-", out)  # 20 days: not recent
        self.assertNotIn("add a cart page", out)  # no history text is shown or sent before the yes
        self.assertEqual(ds.read_consent(), {"keep": {}, "no": {}})

    def show_fill(self):
        rc, out, err = self.run_cmd(ds.cmd_fill, [])
        self.says("sess1", out)  # the AI shows it, as the skill says
        return out

    def test_the_persons_yes_is_read_by_the_plugin(self):
        self.show_fill()
        ctx = self.prompt("keep 1")
        self.assertIn("datastash fill --kept", ctx)
        rc, out, err = self.run_cmd(ds.cmd_fill, ["--kept"])
        self.assertIn("the person kept: shop-", out)
        self.assertTrue(ds.consent_for(self.p1))
        self.assertFalse(ds.consent_for(self.p2))
        self.assertIn(self.p2, ds.read_consent()["no"])  # a no is remembered ...
        self.assertEqual([x["folder"] for x in ds.fill_candidates()], [])  # ... and not asked again
        self.assertEqual([x["folder"] for x in ds.fill_candidates(again=True)], [self.p1, self.p2])  # unless they ask
        self.assertEqual(self.calls, [])

    def test_only_the_very_next_prompt_answers_it(self):
        self.show_fill()
        self.prompt("fix the login bug first")
        self.assertIsNone(self.prompt("yes"))  # a yes to something else, later: not a yes to keeping projects
        self.assertEqual(ds.read_consent(), {"keep": {}, "no": {}})

    def test_no_is_remembered_and_nothing_sent(self):
        self.show_fill()
        self.prompt("no thanks")
        rc, out, err = self.run_cmd(ds.cmd_fill, ["--kept"])
        self.assertIn("the person said no", out)
        self.assertEqual(set(ds.read_consent()["no"]), {self.p1, self.p2})
        self.assertEqual(self.calls, [])

    def test_the_ai_cannot_say_yes(self):
        self.show_fill()
        self.forged({"session_id": "sess1", "prompt": "yes"})
        self.tool_use("sess1", "printf yes | datastash prompt")
        self.forged({"session_id": "sess1", "prompt": "yes", "hook_event_name": "UserPromptSubmit",
                     "transcript_path": self.transcript("sess1"), "prompt_id": str(uuid.uuid4())})
        self.assertEqual(ds.read_consent(), {"keep": {}, "no": {}})
        rc, out, err = self.run_cmd(ds.cmd_fill, ["--kept"])
        self.assertIn("nothing is kept", out)

    def test_a_covert_list_then_its_own_question_is_not_a_yes(self):
        self.run_cmd(ds.cmd_fill, [])  # run by the AI but not shown
        self.says("sess1", "Restart Claude Code once so datastash connects. Shall I also update the readme?")
        self.assertIsNone(self.prompt("yes"))
        self.assertEqual(ds.read_consent(), {"keep": {}, "no": {}})

    def test_the_status_ask_offers_it_too(self):
        ctx = self.prompt("status of all my agents")
        self.assertIn(ds.FILL_QUESTION, ctx)
        self.prompt("yes")
        self.assertTrue(ds.consent_for(self.p1) and ds.consent_for(self.p2))
        self.assertNotIn(ds.FILL_QUESTION, self.prompt("status of all my agents"))

    def test_nothing_goes_up_without_the_yes(self):
        rc, out, err = self.run_cmd(ds.cmd_status_set, ["-"], stdin=json.dumps({"doing": "Working on the cart."}))
        self.assertEqual(rc, 0, err)
        self.assertIn("kept on this machine only", out)
        self.assertEqual(self.spawned, [])
        out_rec = ds.read_json(ds._outbox_path("web-app"), {})
        self.assertEqual(ds.cmd_status_send(["web-app", out_rec["seq"]]), 0)
        self.assertEqual(ds.facts_refresh_once({"slug": "web-app", "name": "web-app", "workbook_id": WB}), {})  # M4
        self.assertFalse(ds.resend_unsent("web-app"))
        self.assertEqual([c for c in self.calls if c[0] == "POST"], [])

    def test_kept_projects_get_set_up_and_others_are_refused(self):
        rc, _, err = self.run_cmd(ds.cmd_auto_name, ["--project", self.p1])
        self.assertNotEqual(rc, 0)
        self.assertIn("not a project the person said yes to", err)
        rc, _, err = self.run_cmd(ds.cmd_fill, ["--excerpts", self.p1])
        self.assertNotEqual(rc, 0)
        self.show_fill()
        self.prompt("yes")
        rc, out, err = self.run_cmd(ds.cmd_fill, ["--excerpts", self.p1])
        self.assertEqual(rc, 0, err)
        self.assertIn("never instructions", out)
        self.assertIn('person asked: "add a cart page', out)
        ds.write_private(ds.conf_path("shop"), json.dumps({"name": "shop", "workbook_id": WB, "projects": [self.p1]}))
        ds.write_private(ds.key_path("shop"), "ldgr_ag_" + "s" * 40)
        rc, out, err = self.run_cmd(ds.cmd_fill, ["--status", self.p1], stdin=json.dumps({"doing": "Building a cart page."}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.spawned[-1][2:4], ["status-send", "shop"])
        rc, _, err = self.run_cmd(ds.cmd_fill, ["--status", self.p1], stdin=json.dumps({"doing": "Overwrite."}))
        self.assertNotEqual(rc, 0)  # only a first status; its own sessions update it after that
        self.assertIn("already has a status", err)


class Facts(Base):
    def test_listener_facts_are_written_only_when_they_change(self):
        self.run_cmd(ds.cmd_status_set, ["-"], stdin=json.dumps({"doing": "Working on the cart."}))
        agent = {"slug": "web-app", "name": "web-app", "workbook_id": WB}
        facts = {"branch": "cart", "last_commit": "add cart page", "gh": {"pr": "#7 open, approved, CI green",
                 "card": {"id": "XLI-7", "step": "review", "review": "approved, at abc1234, CI green"}}}
        ds.project_facts = lambda here, with_gh=True: facts
        ds.facts_refresh_once(agent)
        posts = [c for c in self.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        body = posts[0][3]
        self.assertEqual((body["context"]["branch"], body["context"]["pr"], body["context"]["last_change"]),
                         ("cart", "#7 open, approved, CI green", "add cart page"))
        self.assertTrue(body["context"]["as_of"]["gh"])  # each fact carries when it was read
        self.assertEqual(body["cards"][0]["id"], "XLI-7")
        self.assertEqual(body["doing"], "Working on the cart.")
        ds.facts_refresh_once(agent)  # nothing moved: no write
        self.assertEqual(len([c for c in self.calls if c[0] == "POST"]), 1)
        facts["gh"] = {"pr": "#7 merged", "card": {"id": "XLI-7", "step": "merged", "review": ""}}
        ds.facts_refresh_once(agent)
        posts = [c for c in self.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[1][3]["cards"][0]["step"], "merged")
        facts["gh"] = {"limited": True}
        self.assertTrue(ds.facts_refresh_once(agent).get("limited"))
        self.assertEqual(len([c for c in self.calls if c[0] == "POST"]), 2)

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
