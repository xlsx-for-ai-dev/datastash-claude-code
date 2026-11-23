#!/usr/bin/env python3
"""Offline tests for the agent status, plugin 0.3.0 (XLI-337): the numbered list (bounded, grouped by agent and project,
every value with its age), the local copy (paged reads, 60 s refresh, shared between sessions), the plugin's own fast
answer (no model turn, no network, under 2 seconds), the end-of-turn reminder, the background write (only while keeping
is on), "full context of N", the typed answers "do N", "no to N" and "N: text" (XLI-310 item 2: proved typed, then
sent by the plugin with a typed proof), keeping (the person's typed keep phrase only), "leave <name> off".
No network, no key; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_status.py"""
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness  # noqa: E402  (a throwaway HOME, datastash folder and Claude Code folder; no network; enforced)

BIN = os.path.join(HERE, "..", "plugins", "datastash", "scripts", "datastash")
TMP = harness.CONF
os.environ["DATASTASH_MAIN_DIR"] = os.path.join(harness.FAKE_HOME, "datastash main")
os.environ["DATASTASH_STATUS_RETRIES"] = "0.01,0.01"
os.environ["DATASTASH_VERIFY_WAIT"] = "0.3"
for k in ("DATASTASH_AGENT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR"):
    os.environ.pop(k, None)

loader = importlib.machinery.SourceFileLoader("datastash_status", BIN)
spec = importlib.util.spec_from_loader("datastash_status", loader)
ds = importlib.util.module_from_spec(spec)
# Cross-file isolation (P2 add): test_plugin.py, when it runs in the same process, turns the listener's status refresh off
# for its own plugin copy; this copy keeps the plugin's default (60 s), and every other file's setting stays as it was.
_refresh = os.environ.pop("DATASTASH_STATUS_REFRESH_SECONDS", None)
loader.exec_module(ds)
if _refresh is not None:
    os.environ["DATASTASH_STATUS_REFRESH_SECONDS"] = _refresh

WB = "11111111-2222-3333-4444-555555555555"
ALL_CALLS = []  # every request any test made through the plugin (answers only from the typed path: see ZzAnswerRoute)
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
                                                    "login_token", "this_session", "spawn_detached")}
        self.calls = []
        self.spawned = []
        self.server_rows = [dict(r) for r in EXAMPLE]
        self.fail_with = None
        self.page_size = None  # set: the list answers a page at a time with next_cursor (XLI-332)
        self.fail_page = None  # set: that page (0-based) fails

        def fake_http(method, path, key=None, body=None, form=None, timeout=40):
            self.calls.append((method, path, key, body))
            ALL_CALLS.append((method, path))
            if self.fail_with is not None:
                raise self.fail_with
            if method == "GET" and path.startswith((ds.STATUS_PATH + "?", ds.PERSON_STATUS_PATH + "?")):
                rows = [dict(r) for r in self.server_rows]
                if self.page_size is None:
                    return 200, "application/json", json.dumps({"workbook_id": WB, "statuses": rows})
                q = dict(urllib.parse.parse_qsl(path.split("?", 1)[1]))
                page = int(q.get("cursor", "c0")[1:]) if q.get("cursor", "c0")[1:].isdigit() else 0
                if self.fail_page == page:
                    raise ds.ApiError(503, {"code": "busy"})
                chunk = rows[page * self.page_size:(page + 1) * self.page_size]
                out = {"workbook_id": WB, "seq": 99, "statuses": chunk}
                if (page + 1) * self.page_size < len(rows):
                    out["next_cursor"] = f"c{page + 1}"
                return 200, "application/json", json.dumps(out)
            return 200, "application/json", json.dumps({"ok": True})
        ds.http = fake_http
        ds.read_key = lambda s: "dsat_person" if ds.person_slug(s) else "ldgr_ag_test"
        ds.signed_in = lambda: True
        ds.login_token = lambda force=False: "dsat_person"
        ds._claude_chain = lambda: [[4242, "start-A"]]
        ds.spawn_detached = lambda argv, cwd: self.spawned.append(argv)
        self.folder = os.path.realpath(tempfile.mkdtemp(prefix="proj-", dir=TMP))
        ds.write_private(ds.conf_path("web-app"), json.dumps({"name": "web-app", "workbook_id": WB, "projects": [self.folder]}))
        ds.write_private(ds.key_path("web-app"), "ldgr_ag_" + "x" * 40)
        ds.record_consent([self.folder], [])
        ds.record_keep_all(True, "typed")  # keeping is on (the Keeping tests start without it)
        ds.write_private(ds._written_path("web-app"), "{}")  # this agent already saved a status (seed tests drop it)
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

    def typed(self, sid, text, pid=None, origin=None, source="typed"):
        """A typed entry as Claude Code writes it for a person's prompt (origin kind "human", promptSource "typed")."""
        v = {"type": "user", "promptId": pid or str(uuid.uuid4()), "message": {"role": "user", "content": text},
             "origin": origin if origin is not None else {"kind": "human"}, "promptSource": source}
        for k in ("origin", "promptSource"):
            if v[k] is False:
                del v[k]  # an older Claude Code: no such field
        self.entry(sid, v)

    def says(self, sid, text):
        self.entry(sid, {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})

    def tool_use(self, sid, command):
        self.entry(sid, {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": command}}]}})

    def run_checks(self, start):
        for argv in list(self.spawned[start:]):
            if argv[2] == "status-verify":
                ds.cmd_status_verify([argv[3]])

    def headless(self, sid, text, pid):
        """A recorded `claude -p "<text>"` entry (this box, Claude Code 2.1.322; ids and folder replaced): no origin,
        promptSource "sdk"."""
        self.entry(sid, {"parentUuid": None, "isSidechain": False, "promptId": pid, "type": "user",
                         "message": {"role": "user", "content": text}, "permissionMode": "default", "promptSource": "sdk",
                         "turnOrigin": "sdk", "turnPosition": {"promptIndex": 0, "turnIndex": 1}, "userType": "external",
                         "entrypoint": "sdk-cli", "cwd": "/home/user/proj", "sessionId": sid, "version": "2.1.322"})

    def prompt(self, text, sid="sess1", say=True, writer=None, **typed_kw):
        """As Claude Code does it: the UserPromptSubmit hook runs first, THEN the typed prompt is written to the
        transcript (with the hook's prompt id); the checker then finds it. Claude shows the note in its reply.
        writer: how the entry is written instead (e.g. self.headless); typed_kw: origin / source for typed()."""
        pid = str(uuid.uuid4())
        inp = {"session_id": sid, "prompt": text, "hook_event_name": "UserPromptSubmit", "cwd": self.folder,
               "prompt_id": pid, "transcript_path": self.transcript(sid)}
        n = len(self.spawned)
        v = self.hook(ds.cmd_prompt, inp)
        self.last_hook = v
        blocked = bool(v) and v.get("decision") == "block"
        if blocked:
            ctx = v["reason"]  # the plugin answered itself (shown to the person; no model turn)
        else:
            ctx = v["hookSpecificOutput"]["additionalContext"] if v else None
        if blocked:
            pass  # Claude Code writes no transcript entry for a blocked prompt (promptKept: false)
        elif writer is not None:
            writer(sid, text, pid)
        else:
            self.typed(sid, text, pid, **typed_kw)
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
        return [c for c in self.calls if "/answer" in c[1] or "/undo" in c[1]]


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
        self.assertIn("   main · no PR · tests all pass · last: moved invoices to the new table · 1 h ago", lines)
        self.assertTrue(lines[-1].startswith("5. **docs** — _Nothing in flight since Monday._ · 3 d ago"), text)
        self.assertEqual(numbers["2"], {"agent": "web-app", "project": "web-app", "ask": "q2",
                                        "text": "Change the session length from 1 day to 7 days?", "plain": False})  # XLI-310: effects
        self.assertEqual((numbers["4"]["agent"], numbers["4"]["ask"]), ("api", "q1"))
        self.assertEqual(numbers["5"], {"agent": "docs", "project": "docs", "ask": None})

    def test_fresh_values_are_not_dimmed_and_unknown_is_blank(self):
        r = ds.norm_status_row(row("web-app", "Writing the tests now.", ago=30, project="web-app",
                                   context={"branch": "b", "pr": "#9 open", "as_of": {"gh": NOW - 30}}, asks=["Ship it?"]))
        lines, _ = ds.status_list([r], NOW)
        self.assertEqual(lines[0], "1. **web-app** — Writing the tests now.")
        self.assertEqual(lines[1], "   b · PR #9 open · just now")  # no tests, no last change: left out, never guessed
        r = ds.norm_status_row(row("web-app", "Same.", ago=30, project="web-app",
                                   context={"pr": "#9 open", "as_of": {"gh": NOW - 600}}, asks=["Ship it?"]))
        self.assertIn("PR #9 open", ds.status_list([r], NOW)[0][1])  # no GitHub facts refresher in 0.3.0: no PR age

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
        self.assertEqual(P("do 4"), ("do", 4))
        self.assertEqual(P("status"), ("all", None))
        self.assertEqual(P("Status."), ("all", None))
        self.assertEqual(P("status of api"), ("more", "api"))
        self.assertEqual(P("what's the status of web-app?"), ("more", "web-app"))
        self.assertEqual(P("leave docs off"), ("leave", "docs"))
        self.assertEqual(P("keep docs private"), ("private", "docs"))
        self.assertIsNone(P("4: no, 30 days"))  # no answers in 0.3.0: an ordinary prompt
        self.assertIsNone(P("status of the build is red, can you look"))
        self.assertIsNone(P("2) update the readme\n3) bump the version"))
        for t in ("2-3 more tests please", "2:30 works for me", "5-star ratings only", "1) fix the login bug",
                  "10 - 15 minutes is fine"):
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
        self.assertIn("agent status isn't available on datastash yet; showing the copy on this machine", out[-1])
        self.assertIn("refreshed 10 min ago", out[-1])
        self.fail_with = None
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})  # the door appears: cleared
        self.assertFalse(ds.door_missing())

    def test_refresher_refreshes_on_wake_and_backs_off_on_404(self):
        wake, stop = threading.Event(), threading.Event()
        ds.STATUS_REFRESH_SECONDS, old = 60, ds.STATUS_REFRESH_SECONDS
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

    def edit(self):
        ds.hook_input = lambda: {"session_id": "sess1", "hook_event_name": "PostToolUse", "tool_name": "Edit",
                                 "tool_input": {"file_path": os.path.join(self.folder, "a.txt")}}
        self.run_cmd(ds.cmd_agent_tool, [])

    def test_a_changed_file_or_a_new_ask_is_a_change(self):
        """No git calls: a file edit or a commit this turn (seen from the tool hooks) is the change."""
        self.turn()
        self.tool()
        self.write()
        self.ten_minutes_pass()
        self.turn()
        self.tool()
        self.assertIsNone(self.stop())
        self.edit()
        self.assertEqual(self.stop()["decision"], "block")
        self.write()
        self.ten_minutes_pass()
        self.turn()
        self.tool('git commit -m "x"')
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
        ds.write_private(ds.CONSENT_PATH, json.dumps({"keep": {}, "no": {}}))  # keeping off
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


class Keeping(Base):
    """Addenda 1-4, 9: the typed keep phrase is the yes for this computer's agents; the server's keep value is ignored;
    with keeping off, a status ask goes the ordinary way and ends with the line to type; each session seeds its own
    first status, then the plugin answers itself."""
    def setUp(self):
        super().setUp()
        ds.write_private(ds.CONSENT_PATH, json.dumps({"keep": {}, "no": {}}))  # not asked yet
        self.other = os.path.realpath(tempfile.mkdtemp(prefix="api-", dir=TMP))

    def test_phrase_matches_forgivingly_and_only_whole(self):
        K = ds.keep_phrase
        for t in (ds.KEEP_PHRASE, ds.KEEP_PHRASE.lower() + ".", "keep my agent's status and what I tell them to remember "
                  "in datastash", "Keep my agents’ status and what I tell them to remember in Datastash!",
                  '"Keep my agents status and what I tell them to remember in datastash"'):
            self.assertTrue(K(t), t)
        for t in ("keep my agents status", ds.KEEP_PHRASE + " and also fix the bug", "please " + ds.KEEP_PHRASE,
                  ds.KEEP_PHRASE + "\nand more", None, ""):
            self.assertFalse(K(t), t)

    def test_typed_phrase_turns_keeping_on_for_every_agent_here(self):
        self.assertFalse(ds.consent_for(self.folder))
        ctx = self.prompt(ds.KEEP_PHRASE)
        self.assertIn("datastash keep --on", ctx)
        self.assertEqual(ds.keep_state(), "yes")
        self.assertTrue(ds.consent_for(self.folder) and ds.consent_for(self.other))  # a new project joins too
        rc, out, err = self.run_cmd(ds.cmd_keep, ["--on"])
        self.assertEqual(rc, 0, err)
        self.assertIn('"Keeping status for: web-app."', out)
        self.assertIn("what the person asks you to remember", out)
        self.assertIn("ask for the status again", out)
        self.assertEqual(self.calls, [])  # nothing is sent by the yes itself

    def test_the_ai_cannot_turn_keeping_on(self):
        """M2 for the phrase: a tool call has no typed entry for its prompt id; an old typed entry's id doesn't help."""
        self.typed("sess1", "fix the login bug", pid="11111111-aaaa-bbbb-cccc-000000000001")
        self.tool_use("sess1", "printf ... | datastash prompt")
        for pid in (str(uuid.uuid4()), "11111111-aaaa-bbbb-cccc-000000000001"):
            self.forged({"session_id": "sess1", "prompt": ds.KEEP_PHRASE, "hook_event_name": "UserPromptSubmit",
                         "transcript_path": self.transcript("sess1"), "prompt_id": pid})
        self.forged({"session_id": "sess1", "prompt": ds.KEEP_PHRASE})
        self.assertIsNone(ds.keep_state())
        rc, out, err = self.run_cmd(ds.cmd_keep, ["--on"])
        self.assertIn("no typed keep phrase from the person was confirmed", out)
        self.assertIsNone(ds.keep_state())

    def test_not_signed_in_starts_no_sign_in(self):
        """XLI-390 P1: keep --on never starts a datastash login: no device code, no page, no login step."""
        ds.signed_in = lambda: False
        base = ds.http

        def http(method, path, key=None, body=None, form=None, timeout=40):
            if path == ds.DEVICE_START_PATH:
                self.calls.append((method, path, key, body))
                return 200, "application/json", json.dumps({"user_code": "BCDF-GHJK", "device_code": "dev-123",
                                                            "verification_uri": ds.ORIGIN + "/device", "interval": 5,
                                                            "expires_in": 600})
            return base(method, path, key, body, form, timeout)
        ds.http = http
        saved = (ds._open_browser, ds.subprocess.Popen)
        ds._open_browser = lambda url: True
        ds.subprocess.Popen = lambda *a, **k: None
        try:
            self.prompt(ds.KEEP_PHRASE)
            rc, out, err = self.run_cmd(ds.cmd_keep, ["--on"])
        finally:
            ds._open_browser, ds.subprocess.Popen = saved
        self.assertEqual(rc, 0, err)
        self.assertNotIn("BCDF-GHJK", out)
        self.assertNotIn("datastash login", out)
        self.assertEqual([c for c in self.calls if c[1] == ds.DEVICE_START_PATH], [])
        self.assertEqual(ds.keep_state(), "yes")

    def test_n2_server_keep_value_is_ignored(self):
        """Addendum 6 N2: until the site has a person-only action that sets it, a server `keep` value never turns
        keeping on (or off); only the person's typed words here do."""
        sess = {"slug": "web-app", "name": "web-app", "workbook_id": WB}
        for value in (True, False, None):
            body = {"workbook_id": WB, "statuses": [dict(r) for r in self.server_rows]}
            if value is not None:
                body["keep"] = value
            ds.http = lambda *a, **k: (200, "application/json", json.dumps(body))
            ds.fetch_status(sess)
            self.assertIsNone(ds.keep_state())
            self.assertFalse(ds.consent_for(self.other))
        # an old copy's "server" entry from before this change is not read either
        ds.write_private(ds.CONSENT_PATH, json.dumps({"keep": {}, "no": {}, "server": {"keep": True, "at": 1}}))
        self.assertIsNone(ds.keep_state())
        ds.record_keep_all(True, "server")
        self.assertIsNone(ds.keep_state())
        self.prompt(ds.KEEP_PHRASE)  # the person's typed phrase
        self.assertEqual(ds.keep_state(), "yes")
        body = {"workbook_id": WB, "statuses": [], "keep": False}
        ds.fetch_status(sess)
        self.assertEqual(ds.keep_state(), "yes")

    def test_each_session_seeds_its_own_status_then_the_list_is_instant(self):
        """Addenda 3-4: the first ask after the yes has the agent save its own status in that turn; the next ask is the
        numbered list, from the local copy, in under 2 seconds."""
        self.prompt(ds.KEEP_PHRASE)
        os.replace(ds._written_path("web-app"), os.path.join(TMP, f"old-written-{time.time()}"))
        rc, out, err = self.run_cmd(ds.cmd_keep, ["--on"])
        self.assertIn("has no saved status yet", out)
        self.assertIn("datastash status-set -", out)
        ctx = self.prompt("What's the status of all my agents?")
        self.assertIn(ds.SEED_NOTE, ctx)
        rc, out, err = self.run_cmd(ds.cmd_status_set, ["-"], stdin=json.dumps({"doing": "Fixing the cart page totals."}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.spawned[-1][2:4], ["status-send", "web-app"])  # kept: sent in the background
        self.calls.clear()
        t = time.time()
        ctx = self.prompt("What's the status of all my agents?")
        self.assertLess(time.time() - t, 2.0)
        self.assertEqual(self.last_hook["decision"], "block")  # the plugin answered itself
        self.assertIn("Fixing the cart page totals.", ctx)
        self.assertTrue(ctx.startswith(ds.STATUS_HEAD))
        self.assertNotIn(ds.SEED_NOTE, ctx)
        self.assertEqual(self.calls, [])

    def test_a_projects_no_wins_over_the_computers_yes(self):
        ds.record_consent([], [self.other])
        self.prompt(ds.KEEP_PHRASE)
        self.assertTrue(ds.consent_for(self.folder))
        self.assertFalse(ds.consent_for(self.other))


class PersonOnly(Base):
    """Addendum 6 N1: a prompt counts as the person's only when its transcript entry has origin.kind "human" AND
    promptSource "typed" or "queued"; anything else (a headless `claude -p`, a notification, a peer message, or an
    entry without the fields) is refused, for every person-only phrase."""
    def setUp(self):
        super().setUp()
        ds.write_private(ds.CONSENT_PATH, json.dumps({"keep": {}, "no": {}}))  # not asked yet

    NOT_PERSON = (
        {"origin": False, "source": False},  # an older Claude Code: neither field
        {"origin": False, "source": "typed"},  # no origin
        {"origin": {"kind": "human"}, "source": False},  # no promptSource
        {"origin": {"kind": "human"}, "source": "sdk"},
        {"origin": {"kind": "task-notification", "producer": "session-task"}, "source": "system"},
        {"origin": {"kind": "peer", "from": "unknown"}, "source": "system"},
        {"origin": {"kind": "human"}, "source": "system"},
        {"origin": "human", "source": "typed"},
    )

    def test_recorded_headless_keep_phrase_is_refused(self):
        """EPM's P1: `claude -p "Keep my agents' status ..."` in any folder: genuine hook input, sdk entry, same id."""
        ctx = self.prompt(ds.KEEP_PHRASE, writer=self.headless)
        self.assertIn("datastash keep --on", ctx)  # the hook can't tell yet; the checker decides
        self.assertIsNone(ds.keep_state())
        self.assertFalse(ds.consent_for(self.folder))
        rc, out, err = self.run_cmd(ds.cmd_keep, ["--on"])
        self.assertIn("no typed keep phrase from the person was confirmed", out)
        self.assertEqual(self.outcomes(), [ds.NOT_PERSON_LINE])  # one line, in case it really was the person
        self.assertEqual(self.calls, [])

    def test_entries_missing_or_with_other_origins_are_refused(self):
        for kw in self.NOT_PERSON:
            self.prompt(ds.KEEP_PHRASE, **kw)
            self.assertIsNone(ds.keep_state(), kw)
            self.assertEqual(self.outcomes(), [ds.NOT_PERSON_LINE], kw)

    def test_typed_and_queued_are_the_person(self):
        self.prompt(ds.KEEP_PHRASE, source="queued")
        self.assertEqual(ds.keep_state(), "yes")
        self.assertEqual(self.outcomes(), [])

    def test_unit_person_origin(self):
        human = {"type": "user", "origin": {"kind": "human"}}
        self.assertTrue(ds._person_origin(dict(human, promptSource="typed")))
        self.assertTrue(ds._person_origin(dict(human, promptSource="queued")))
        for v in ({"type": "user"}, dict(human), dict(human, promptSource="sdk"), {"promptSource": "typed"},
                  {"origin": {"kind": "task-notification"}, "promptSource": "typed"}, {"origin": None, "promptSource": "typed"}):
            self.assertFalse(ds._person_origin(v), v)


class LinkGate(Base):
    """Addendum 6 N3: `link --project DIR` gets the same named-yes gate as `auto-name --project`."""
    def test_link_another_folder_needs_keeping_on_and_not_left_off(self):
        """Review 1 LOW 3: the keep phrase covers all agents on this computer, so it is the yes for its folders; a
        folder the person left off, or keeping off, refuses (no pointer to a removed skill section)."""
        other = os.path.realpath(tempfile.mkdtemp(prefix="other-", dir=TMP))
        left = os.path.realpath(tempfile.mkdtemp(prefix="left-", dir=TMP))
        ds.record_left_off([left])
        rc, _, err = self.run_cmd(ds.cmd_link, ["web-app", "--workbook", WB, "--project", left])
        self.assertNotEqual(rc, 0)
        self.assertIn("is not kept in datastash", err)
        self.assertNotIn("Recent projects from history", err)
        self.assertNotIn(left, ds.read_json(ds.conf_path("web-app"), {})["projects"])
        rc, _, err = self.run_cmd(ds.cmd_link, ["web-app", "--workbook", WB, "--project", other])
        self.assertEqual(rc, 0, err)
        self.assertIn(other, ds.read_json(ds.conf_path("web-app"), {})["projects"])
        c = ds.read_json(ds.CONSENT_PATH, {})
        c["all"] = {"keep": False, "at": 1}
        ds.write_private(ds.CONSENT_PATH, json.dumps(c))
        third = os.path.realpath(tempfile.mkdtemp(prefix="third-", dir=TMP))
        rc, _, err = self.run_cmd(ds.cmd_auto_name, ["--project", third])
        self.assertNotEqual(rc, 0)
        self.assertIn("keeping is not on for this computer", err)


class CliSpeed(Base):
    def test_status_all_from_the_cli_is_fast(self):
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        env = dict(os.environ)
        t = time.time()
        r = subprocess.run([sys.executable, "-I", BIN, "status", "--all"], capture_output=True, text=True, env=env, timeout=20)
        took = time.time() - t
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1. **web-app**", r.stdout)
        self.assertLess(took, 2.0)
        print(f"\n  datastash status --all from the local copy: {took * 1000:.0f} ms", file=sys.stderr)


class Paging(Base):
    """XLI-332: the list is read a page at a time, following next_cursor to the end; the copy is replaced only after a
    whole walk."""
    def rows(self, n):
        return [row(f"agent-{i:02d}", f"Working on part {i}.", ago=60 + i, project=f"p{i}") for i in range(n)]

    def test_three_pages_are_walked_and_all_rows_saved(self):
        self.server_rows, self.page_size = self.rows(7), 3
        c = ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        gets = [x for x in self.calls if x[0] == "GET"]
        self.assertEqual(len(gets), 3)
        self.assertNotIn("cursor", gets[0][1])
        self.assertIn("cursor=c1", gets[1][1])
        self.assertIn("cursor=c2", gets[2][1])
        self.assertEqual(sorted(r["agent"] for r in c["agents"]), sorted(f"agent-{i:02d}" for i in range(7)))

    def test_a_failure_partway_keeps_the_old_copy_and_marks_it(self):
        ds.save_status_cache(WB, [ds.norm_status_row(EXAMPLE[0])], time.time() - 300, merge=False)
        self.server_rows, self.page_size, self.fail_page = self.rows(7), 3, 1
        with self.assertRaises(ds.ApiError):
            ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        c = ds.read_status_cache(WB)
        self.assertEqual([r["agent"] for r in c["agents"]], ["docs"])  # never a half walk as the copy
        self.assertTrue(c["failed_at"] and c["failed_at"] > c["fetched_at"])

    def test_a_cursor_restart_note_walks_again_from_page_one_once(self):
        state = {"n": 0}
        rows = [dict(r) for r in self.rows(4)]

        def http(method, path, key=None, body=None, form=None, timeout=40):
            self.calls.append((method, path, key, body))
            state["n"] += 1
            if "cursor=" not in path:
                return 200, "application/json", json.dumps({"statuses": rows[:2], "next_cursor": "c1"})
            if state["n"] == 2:  # the server could not read the cursor: page 1 again, with a note
                return 200, "application/json", json.dumps({"statuses": rows[:2], "next_cursor": "c1", "note": "x"})
            return 200, "application/json", json.dumps({"statuses": rows[2:]})
        ds.http = http
        c = ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        self.assertEqual(len(c["agents"]), 4)  # no row twice

    def test_the_copy_is_private_and_a_link_is_refused(self):
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        path = ds.status_cache_path(WB)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        target = path + ".real"
        os.replace(path, target)
        os.symlink(target, path)
        self.assertIsNone(ds.read_status_cache(WB))


class Morning(Base):
    """XLI-359: the person's first status of the day starts with the server's morning lines (hours saved yesterday, then
    new agents waiting for the OK); once per local day; none when the server sent none; an agent key's read keeps them."""
    LINES = ["Yesterday datastash saved you 1.2 hours across all your agents.", "New agents waiting for your OK:",
             "Builder (Laptop)", "https://x.example/agents"]

    def setUp(self):
        super().setUp()
        if os.path.exists(ds.MORNING_PATH):
            os.replace(ds.MORNING_PATH, os.path.join(TMP, f"old-morning-{time.time()}"))
        self.today = time.strftime("%Y-%m-%d", time.localtime())
        self.morning = {"day": self.today, "lines": list(self.LINES), "show": "x"}
        base = ds.http

        def http(method, path, key=None, body=None, form=None, timeout=40):
            st, ct, text = base(method, path, key, body, form, timeout)
            if method == "GET" and path.startswith(ds.PERSON_STATUS_PATH + "?") and self.morning is not None:
                b = json.loads(text)
                b["morning"] = self.morning
                text = json.dumps(b)
            return st, ct, text
        ds.http = http

    def person_fetch(self):
        return ds.fetch_status({"slug": ds.PERSON_SLUG, "workbook_id": WB})

    def test_person_read_sends_the_offset_and_the_first_status_of_the_day_starts_with_the_lines_once(self):
        self.person_fetch()
        get = [c for c in self.calls if c[0] == "GET"][0][1]
        q = dict(urllib.parse.parse_qsl(get.split("?", 1)[1]))
        self.assertEqual(int(q["utc_offset"]), int(time.localtime().tm_gmtoff // 60))
        out = ds.render_status_all(self.folder, ds.this_session(), for_person=True)
        self.assertEqual(out[:4], self.LINES)
        self.assertTrue(out[4].startswith(ds.STATUS_HEAD))
        again = ds.render_status_all(self.folder, ds.this_session(), for_person=True)
        self.assertTrue(again[0].startswith(ds.STATUS_HEAD))
        self.assertNotIn(self.LINES[0], "\n".join(again))
        self.assertNotIn(self.LINES[0], "\n".join(ds.render_status_all(self.folder, ds.this_session())))

    def test_the_ai_facing_status_also_starts_with_them_before_the_framed_block(self):
        self.person_fetch()
        out = ds.render_status_all(self.folder, ds.this_session())
        self.assertEqual(out[:4], self.LINES)
        self.assertTrue(out[4].startswith("<<datastash-data "))

    def test_no_lines_on_a_day_the_server_sent_none_or_for_another_day(self):
        self.morning = None
        self.person_fetch()
        self.assertTrue(ds.render_status_all(self.folder, ds.this_session(), for_person=True)[0].startswith(ds.STATUS_HEAD))
        self.morning = {"day": "2001-01-01", "lines": list(self.LINES)}
        self.person_fetch()
        self.assertTrue(ds.render_status_all(self.folder, ds.this_session(), for_person=True)[0].startswith(ds.STATUS_HEAD))
        self.assertFalse(os.path.exists(ds.MORNING_PATH))

    def test_an_agent_key_read_keeps_the_persons_lines_and_never_asks_with_an_offset(self):
        self.person_fetch()
        self.calls.clear()
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        self.assertNotIn("utc_offset", [c for c in self.calls if c[0] == "GET"][0][1])
        self.assertEqual(ds.render_status_all(self.folder, ds.this_session(), for_person=True)[:4], self.LINES)

    def test_bad_lines_from_the_server_are_ignored_and_control_characters_dropped(self):
        for bad in ({"day": self.today, "lines": "text"}, {"day": "today", "lines": ["x"]}, {"day": self.today, "lines": [1]}, []):
            self.morning = bad
            self.person_fetch()
            self.assertTrue(ds.render_status_all(self.folder, ds.this_session(), for_person=True)[0].startswith(ds.STATUS_HEAD))
        self.morning = {"day": self.today, "lines": ["Yesterday datastash saved you 0.5 hours\x1b[2J across all your agents."]}
        self.person_fetch()
        self.assertEqual(ds.render_status_all(self.folder, ds.this_session(), for_person=True)[0],
                         "Yesterday datastash saved you 0.5 hours [2J across all your agents.")


class AskedRead(Base):
    """XLI-359: only a read someone asked for says asked=1; the background refresh never does."""
    def test_on_demand_read_says_asked_and_the_refresh_does_not(self):
        ds.save_status_cache(WB, [ds.norm_status_row(EXAMPLE[0])], time.time() - 600, merge=False)
        ds.render_status_all(self.folder, ds.this_session())  # stale copy: one live read, asked for
        gets = [c[1] for c in self.calls if c[0] == "GET"]
        self.assertEqual(len(gets), 1)
        self.assertIn("asked=1", gets[0])
        self.calls.clear()
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})  # what the refresher calls
        ds.fetch_status({"slug": ds.PERSON_SLUG, "workbook_id": WB})
        self.assertTrue(all("asked=" not in c[1] for c in self.calls if c[0] == "GET"))


class Refresh(Base):
    def run_refresher(self, seconds=0.3):
        wake, stop = threading.Event(), threading.Event()
        th = threading.Thread(target=ds.status_refresher, args=({"slug": "web-app", "workbook_id": WB}, wake, stop), daemon=True)
        th.start()
        time.sleep(seconds)
        stop.set()
        wake.set()
        th.join(2)

    def test_every_60_seconds_by_default(self):
        self.assertEqual(ds.STATUS_REFRESH_SECONDS, 60.0)  # DATASTASH_STATUS_REFRESH_SECONDS unset in this run

    def test_skips_when_another_session_refreshed_the_shared_copy(self):
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        self.calls.clear()
        self.run_refresher()
        self.assertEqual(self.calls, [])  # the copy is under 50 s old: no call
        ds.save_status_cache(WB, [], time.time() - 55, merge=False)
        self.run_refresher()
        self.assertEqual(len([c for c in self.calls if c[0] == "GET"]), 1)

    def test_nothing_is_read_while_keeping_is_off(self):
        ds.write_private(ds.CONSENT_PATH, json.dumps({"keep": {}, "no": {}}))
        self.run_refresher()
        self.assertEqual(self.calls, [])


class FastAnswer(Base):
    """XLI-337: with keeping on and this agent's status saved, the prompt hook answers itself from the local copy:
    a block with the list as the shown text (no model turn), no network call."""
    def setUp(self):
        super().setUp()
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        ds.write_private(ds.INSTALL_ASKED_PATH, "{}")  # the install question was shown already (InstallQuestion tests it)
        self.calls.clear()

    def test_status_of_all_my_agents_is_answered_by_the_plugin(self):
        for phrase in ("status of all my agents", "What's the status of all my agents?", "status"):
            text = self.prompt(phrase)
            self.assertEqual(self.last_hook, {"decision": "block", "reason": text, "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit", "suppressOriginalPrompt": True}}, phrase)
            self.assertTrue(text.startswith("Status of your agents (refreshed"), text)
            self.assertIn(ds.STATUS_WORDS_LINE, text)  # each agent's text is its own words
            self.assertIn("1. web-app — ", text)
            self.assertIn("Ask: Delete the old /invoices endpoint", text)
            self.assertNotIn("**", text)  # plain text: the person reads it as it is
            self.assertNotIn("<<datastash-data", text)
        self.assertEqual(self.calls, [])

    def test_status_of_one_agent(self):
        text = self.prompt("status of api")
        self.assertIn("api — Billing refactor", text)
        self.assertNotIn("web-app", text)
        self.assertIsNone(self.prompt("status of nobody-here"))  # no such agent: an ordinary prompt
        self.assertIsNone(self.last_hook)

    def test_full_context_of_n_from_the_local_copy(self):
        self.prompt("status of all my agents")
        text = self.prompt("full context of 1")
        self.assertTrue(text.startswith("Status of your agents: full context of 1"))
        self.assertIn("Tried refresh on activity", text)
        text = self.prompt("full context of 4")
        self.assertIn("Ask from api", text)
        self.assertIn("Nothing calls it.", text)
        self.assertIsNone(self.prompt("full context of 40"))  # no such number: an ordinary prompt
        self.assertIsNone(self.prompt("full context of 1", sid="sess2"))  # another session's list never counts
        self.assertEqual(self.calls, [])

    def test_offline_shows_the_copy_with_its_age(self):
        ds.save_status_cache(WB, [ds.norm_status_row(r) for r in EXAMPLE], time.time() - 600, merge=False)
        ds.note_refresh_failed(WB)
        text = self.prompt("status of all my agents")
        self.assertIn("refreshed 10 min ago; the last refresh failed just now; offline", text.splitlines()[0])

    def test_door_404_says_not_available_yet_never_sent(self):
        self.fail_with = ds.ApiError(404, {"error": "not_found"})
        with self.assertRaises(ds.ApiError):
            ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        text = self.prompt("status of all my agents")
        self.assertIn("agent status isn't available on datastash yet", text)
        self.assertNotIn("sent", text.lower())

    def test_blocked_answer_still_shows_the_reconnect_line_when_due(self):
        """Review 1 LOW 1: a status phrase answered by the plugin still shows "signed in now, reconnect" once."""
        ds.ensure_dirs()
        ds.write_private(ds._signed_out_mark("sess1"), "{}")
        saved = ds._login_recorded
        ds._login_recorded = lambda: True
        try:
            text = self.prompt("status of all my agents")
        finally:
            ds._login_recorded = saved
        self.assertTrue(text.startswith(ds.STATUS_HEAD))
        self.assertTrue(text.rstrip().endswith(ds.CONNECT_LINE), text)
        self.assertFalse(os.path.exists(ds._signed_out_mark("sess1")))
        self.assertNotIn(ds.CONNECT_LINE, self.prompt("status of all my agents"))  # once
        self.assertEqual(self.calls, [])

    def test_main_session_is_fast_only_with_keeping_on_and_a_saved_status(self):
        """Review 1 LOW 2: the person's main session gets the same checks: keeping on and a saved status (local copy)."""
        ds.ensure_dirs()
        ds.write_private(ds._main_path("main1"), "{}")
        text = self.prompt("status of all my agents", sid="main1")
        self.assertEqual(self.last_hook["decision"], "block")
        self.assertTrue(text.startswith(ds.STATUS_HEAD))
        c = ds.read_json(ds.CONSENT_PATH, {})
        c["all"] = {"keep": False, "at": 1}
        ds.write_private(ds.CONSENT_PATH, json.dumps(c))
        ctx = self.prompt("status", sid="main1")
        self.assertIsNone(self.last_hook.get("decision"))
        self.assertTrue(ctx.rstrip().endswith(ds.TURN_ON_LINE))
        ds.record_keep_all(True, "typed")
        for n in os.listdir(ds.CONF):
            if re.fullmatch(r"status-[A-Za-z0-9-]+\.json", n) and n != "status-consent.json":
                os.replace(os.path.join(ds.CONF, n), os.path.join(TMP, f"gone-{time.time()}-{n}"))
        self.assertIsNone(self.prompt("status", sid="main1"))  # no saved status: an ordinary prompt
        self.assertIsNone(self.last_hook)
        self.assertEqual(self.calls, [])

    def test_under_two_seconds_in_process(self):
        t = time.time()
        self.prompt("status of all my agents", say=False)
        self.assertLess(time.time() - t, 2.0)


class DoN(Base):
    """An ask with no server instance (an older list) or with effects: exactly as in 0.9.8. "do N" shows the ask and
    says to tell the agent in its own session; "no to N" and "N: text" are ordinary prompts. Nothing is sent, no checker."""
    def setUp(self):
        super().setUp()
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        ds.write_private(ds.INSTALL_ASKED_PATH, "{}")
        self.prompt("status of all my agents")
        self.calls.clear()

    def test_do_n_for_an_ask_shows_it_and_sends_nothing(self):
        n = self.spawned_before = len(self.spawned)
        text = self.prompt("do 4")
        self.assertEqual(text, 'Answering asks from here is coming. Ask 4, from api: "Delete the old /invoices endpoint?". '
                               "To answer now, tell api in its own session.")  # 0.9.8's text, word for word
        self.assertEqual(self.last_hook["decision"], "block")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.spawned[n:], [])  # no checker, no sender

    def test_do_n_that_is_not_an_ask_or_with_no_list_is_ordinary(self):
        self.assertIsNone(self.prompt("do 1"))  # number 1 is an agent, not an ask
        self.assertIsNone(self.prompt("do 4", sid="sess2"))  # this session showed no list
        self.assertIsNone(self.prompt("no to 1"))
        self.assertIsNone(self.prompt("1: no, keep it"))
        self.assertIsNone(self.prompt("4: no, keep it"))  # 0.9.8: an ordinary prompt, to the AI
        self.assertIsNone(self.prompt("no to 4"))
        self.assertEqual(self.calls, [])


KEY = "dsak_" + "k" * 43
GRANT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
I_PLAIN, I_OPTS, I_EFFECT, I_NP = ("99999999-8888-4777-8666-55555555555" + c for c in "1234")
TYPED_ROWS = [
    row("api", "Waiting on the person.", project="api", asks=[
        {"id": "p1", "text": "Ship the fix today?", "details": "All tests pass.", "instance": I_PLAIN},
        {"id": "p2", "text": "Which region?", "options": ["eu-west", {"label": "US-East"}], "instance": I_OPTS},
        {"id": "p3", "text": "Delete the old endpoint?", "touches": "src/routes.ts", "if_yes": "removes it",
         "if_no": "keeps it", "instance": I_EFFECT},
        {"id": "p4", "text": "Rotate the key?", "needs_person": True, "instance": I_NP}]),
]


def server_mac(key, grant, wb, instance, answer, p):
    """The MAC as the server computes it (src/agents/answers.ts checkTypedProof), written here independently: the answer
    as Node's JSON.stringify writes it (non-ASCII kept, no spaces), then HMAC-SHA256 with the whole key string."""
    import hashlib
    import hmac
    js = json.dumps(answer, ensure_ascii=False, separators=(",", ":"))
    lines = ["datastash-typed-answer/v1", grant.lower(), wb.lower(), instance.lower(),
             hashlib.sha256(js.encode()).hexdigest(), p["prompt_id"], p["typed_sha256"], str(p["typed_at"]), str(p["iat"])]
    return hmac.new(key.encode(), "\n".join(lines).encode(), "sha256").hexdigest()


class TypedAnswer(Base):
    """XLI-310 item 2 (plugin 0.9.9): the person types "do N", "no to N" or "N: text" against the numbered list this
    session showed; the transcript checker proves it was typed by them; then the plugin itself (never the model) posts the
    answer with a typed proof signed with the login's answer key. A missing key or an ask that is not plain goes exactly
    as in 0.9.8 (M1)."""
    def setUp(self):
        super().setUp()
        self.server_rows = [dict(r) for r in TYPED_ROWS]
        self.key = (KEY, GRANT)
        self.saved_k = (ds.read_answer_key, ds._open_browser, ds.answer_key_kept)
        ds.read_answer_key = lambda: self.key
        ds.answer_key_kept = lambda: self.key is not None
        self.browser = []
        ds._open_browser = lambda url: self.browser.append(url) or True
        self.addCleanup(self.restore_k)
        self.answer_reply = (200, {"how": "click_free", "delivered": True})
        fake = ds.http

        def answer_http(method, path, key=None, body=None, form=None, timeout=40):
            if path != ds.PERSON_ANSWER_PATH:
                return fake(method, path, key=key, body=body, form=form, timeout=timeout)
            self.calls.append((method, path, key, body))
            ALL_CALLS.append((method, path, key, body))
            status, out = self.answer_reply
            if status >= 400:
                raise ds.ApiError(status, out)
            return status, "application/json", json.dumps(out)
        ds.http = answer_http
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        ds.write_private(ds.INSTALL_ASKED_PATH, "{}")
        self.prompt("status of all my agents")
        self.calls.clear()
        nums = ds.read_view("sess1")["numbers"]
        self.n = {v["ask"]: int(k) for k, v in nums.items() if isinstance(v, dict) and v.get("ask")}

    def restore_k(self):
        ds.read_answer_key, ds._open_browser, ds.answer_key_kept = self.saved_k

    def sent(self):
        return [c for c in self.calls if c[1] == ds.PERSON_ANSWER_PATH]

    def test_typed_do_n_is_proved_then_sent_by_the_plugin_with_a_mac_the_server_accepts(self):
        n = self.n["p1"]
        note = self.prompt(f"do {n}")
        self.assertNotEqual((self.last_hook or {}).get("decision"), "block")  # passes through: the transcript gets it
        self.assertIn("never call", note.lower())
        self.assertIn("ledger_agent_status", note)
        [(method, path, key, body)] = self.sent()
        self.assertEqual((method, key), ("POST", "dsat_person"))  # the login token as bearer; never the answer key
        self.assertEqual(set(body), {"workbook_id", "instance", "answer", "typed_proof"})
        self.assertEqual((body["workbook_id"], body["instance"], body["answer"]), (WB, I_PLAIN, "yes"))
        p = body["typed_proof"]
        self.assertEqual(set(p), {"v", "prompt_id", "typed_sha256", "typed_at", "iat", "mac"})
        self.assertEqual(p["v"], 1)
        import hashlib
        self.assertEqual(p["typed_sha256"], hashlib.sha256(f"do {n}".encode()).hexdigest())
        self.assertTrue(0 <= p["iat"] - p["typed_at"] <= 125_000)
        self.assertLessEqual(abs(time.time() * 1000 - p["iat"]), 60_000)
        self.assertRegex(p["prompt_id"], "^" + ds.PROMPT_ID_RE + "$")
        self.assertEqual(p["mac"], server_mac(KEY, GRANT, WB, I_PLAIN, "yes", p))
        self.assertEqual(self.outcomes(), [f'Sent "yes" to api.'])

    def test_no_to_n_and_n_colon_text(self):
        n = self.n["p1"]
        self.prompt(f"no to {n}")
        self.prompt(f"Don't do {n}")
        self.prompt(f"{n}: Not today, wait for Monday")
        answers = [c[3]["answer"] for c in self.sent()]
        self.assertEqual(answers, ["no", "no", "Not today, wait for Monday"])  # the person's own words, case kept
        for c in self.sent():
            self.assertEqual(c[3]["typed_proof"]["mac"], server_mac(KEY, GRANT, WB, I_PLAIN, c[3]["answer"], c[3]["typed_proof"]))

    def test_options_must_match_exactly_one(self):
        n = self.n["p2"]
        self.prompt(f"{n}: us-east")
        self.assertEqual([c[3]["answer"] for c in self.sent()], ["US-East"])  # the option as the agent wrote it
        self.calls.clear()
        self.prompt("status of all my agents")
        self.calls.clear()
        n = {v["ask"]: int(k) for k, v in ds.read_view("sess1")["numbers"].items() if v.get("ask")}["p2"]
        text = self.prompt(f"{n}: asia")
        self.assertEqual(self.last_hook["decision"], "block")
        self.assertIn("eu-west", text)
        self.assertIn("US-East", text)
        self.assertEqual(self.sent(), [])

    def test_headless_ai_typed_system_and_reused_prompts_send_nothing(self):
        n = self.n["p1"]
        self.prompt(f"do {n}", writer=self.headless)  # claude -p: promptSource "sdk", no origin
        self.assertEqual(self.outcomes(), [ds.NOT_PERSON_LINE])
        self.prompt(f"do {n}", origin={"kind": "system"})  # a task notification or a peer message
        self.prompt(f"do {n}", origin=False, source=False)  # an older Claude Code: no origin fields
        pid = str(uuid.uuid4())  # the AI pipes a hook-shaped prompt in a tool call: no typed entry is written
        self.forged({"session_id": "sess1", "prompt": f"do {n}", "hook_event_name": "UserPromptSubmit", "cwd": self.folder,
                     "prompt_id": pid, "transcript_path": self.transcript("sess1")})
        self.typed("sess1", f"do {n} later")  # a typed entry with other words under another id proves nothing
        self.assertEqual(self.sent(), [])
        # the same typed prompt id is used once only
        self.prompt("status of all my agents")  # the list again (it answers for 3 prompts)
        self.prompt(f"do {n}")
        self.assertEqual(len(self.sent()), 1)
        rec = {"kind": "answer", "sid": "sess1", "tp": self.transcript("sess1"), "pid": self.sent()[0][3]["typed_proof"]["prompt_id"],
               "prompt": f"do {n}", "at": time.time(), "data": {"workbook": WB, "instance": I_PLAIN, "answer": "yes", "agent": "api"}}
        ds.write_private(ds._pending_path(rec["pid"]), json.dumps(rec))
        ds.cmd_status_verify([rec["pid"]])
        self.assertEqual(len(self.sent()), 1)

    def assert_like_098(self, n, ask_text):
        """M1: 0.9.8's behaviour: "do N" shows the old answer (blocked); "no to N" and "N: text" reach the AI as ordinary
        prompts with nothing added; no checker starts and nothing is sent."""
        s = len(self.spawned)
        text = self.prompt(f"do {n}")
        self.assertEqual(self.last_hook["decision"], "block")
        self.assertEqual(text, 'Answering asks from here is coming. Ask %d, from api: "%s". '
                               "To answer now, tell api in its own session." % (n, ask_text))
        for typed in (f"no to {n}", f"{n}: Not today, wait for Monday", f"{n}: US-East"):
            self.prompt("status of all my agents")  # a fresh list each time: never ordinary only because it is stale
            self.assertEqual(ds.read_view("sess1")["numbers"][str(n)].get("ask") is not None, True)
            self.assertIsNone(self.prompt(typed), typed)
            self.assertIsNone(self.last_hook, typed)
        self.assertEqual(self.spawned[s:], [])
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.outcomes(), [])

    def test_a_missing_key_goes_exactly_as_in_098(self):
        self.key = None
        self.assert_like_098(self.n["p1"], "Ship the fix today?")

    def test_a_key_lost_before_the_check_sends_nothing(self):
        n = self.n["p1"]
        ds.spawn_detached = lambda argv, cwd: self.spawned.append(argv)
        s = len(self.spawned)
        pid = str(uuid.uuid4())
        inp = {"session_id": "sess1", "prompt": f"do {n}", "hook_event_name": "UserPromptSubmit", "cwd": self.folder,
               "prompt_id": pid, "transcript_path": self.transcript("sess1")}
        self.hook(ds.cmd_prompt, inp)
        self.typed("sess1", f"do {n}", pid)
        self.key = None
        self.run_checks(s)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.outcomes(), [ds.ANSWER_NO_KEY_LINE])
        self.assertNotIn("click", ds.ANSWER_NO_KEY_LINE)

    def test_an_effect_ask_or_needs_person_falls_back(self):
        for ask in ("p3", "p4"):
            with self.subTest(ask=ask):
                self.prompt("status of all my agents")
                n = {v["ask"]: int(k) for k, v in ds.read_view("sess1")["numbers"].items() if v.get("ask")}[ask]
                self.assert_like_098(n, ds.read_view("sess1")["numbers"][str(n)]["text"])

    def test_held_changed_and_failed_answers_tell_the_person(self):
        n = self.n["p1"]
        url = ds.ORIGIN + "/confirm/abc123"
        self.answer_reply = (403, {"code": "person_approval_needed", "approve_url": url})
        self.prompt(f"do {n}")
        self.assertEqual(self.outcomes(), [f"api's ask needs your OK on the datastash site: {url}"])
        self.assertEqual(self.browser, [url])
        self.answer_reply = (409, {"code": "ask_changed"})
        self.prompt("status of all my agents")
        self.prompt(f"do {n}")
        self.assertEqual(self.outcomes(), [ds.ANSWER_CHANGED_LINE])
        self.answer_reply = (200, {"code": "person_approval_needed", "approve_url": "https://evil.example/x"})
        self.prompt("status of all my agents")
        self.prompt(f"do {n}")
        [line] = self.outcomes()
        self.assertNotIn("evil.example", line)

    def test_no_token_or_key_in_anything_shown(self):
        n = self.n["p1"]
        shown = [self.prompt(f"do {n}"), json.dumps(self.last_hook)] + self.outcomes()
        self.prompt("status of all my agents")
        self.prompt(f"no to {n}", writer=self.headless)
        shown += [json.dumps(self.last_hook)] + self.outcomes()
        for t in shown:
            self.assertNotIn("dsak_", t)
            self.assertNotIn("dsat_", t)
            self.assertNotIn(KEY, t)

    def test_an_old_list_with_no_instance_falls_back(self):
        self.server_rows = [row("api", "x", project="api", asks=[{"id": "p9", "text": "Go?"}])]
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        self.prompt("status of all my agents")
        n = {v["ask"]: int(k) for k, v in ds.read_view("sess1")["numbers"].items() if v.get("ask")}["p9"]
        self.assert_like_098(n, "Go?")


class ProofBytes(unittest.TestCase):
    """The MAC covers sha256(JSON.stringify(answer)) exactly as Node writes it. Vectors made with node 22 from the
    server's own formula (key dsak_k*43, grant/workbook/instance below, prompt-0001-abcdef, sha256("do 3"),
    typed_at 1700000000000, iat 1700000003000)."""
    VECTORS = [
        ("yes", "0127c8a51702bf8988fa705b0df48293f62441d5e57d81f012f5ec86d4712c2d"),
        ('Ship it "now" \\ ok\u0001\u001f\t\n\u007f \u00e9 \u65e5\u672c \U0001F600 \u2028 /x',
         "3172f4fdfd191557f83aa48a3f5b80117646527dfd1fd66f355bd68eb8130288"),
        ("no", "a98a5490c078f158d1100e9e1f6f6f53a3ce7e319b5a71370a0e71d356c7db9f"),
        ("\ud800lone", "62f6f3d501dcd6c5d231a02532b9b5f587b336b34bb2f4f44a12326bc88851ef"),
    ]

    def proof(self, answer):
        import hashlib
        return ds.typed_proof(KEY, "AAAAAAAA-bbbb-4ccc-8ddd-eeeeeeeeeeee", WB, "99999999-8888-4777-8666-555555555555", answer,
                              "prompt-0001-abcdef", "do 3", 1700000000000, 1700000003000)

    def test_node_vectors(self):
        for answer, mac in self.VECTORS:
            with self.subTest(answer=answer):
                p = self.proof(answer)
                self.assertEqual(p["mac"], mac)
                self.assertEqual((p["v"], p["typed_at"], p["iat"], p["prompt_id"]), (1, 1700000000000, 1700000003000, "prompt-0001-abcdef"))

    def test_js_json_matches_node(self):
        import shutil
        node = shutil.which("node")
        if node is None:
            self.skipTest("no node here")
        samples = ["yes", "a\u0000b\u0008\u000c\u000b", "\u2028\u2029\u007f\u0080", "\U0001F600\udfff\ud83d", '"\\/']
        r = subprocess.run([node, "-e", "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{for(const s of JSON.parse(d))console.log(JSON.stringify(JSON.stringify(s)))})"],
                           input=json.dumps(samples), capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([json.loads(x) for x in r.stdout.split("\n") if x], [ds.js_json(s) for s in samples])


class StatusOff(Base):
    def setUp(self):
        super().setUp()
        ds.write_private(ds.CONSENT_PATH, json.dumps({"keep": {}, "no": {}}))  # no keep phrase yet

    def test_status_goes_the_ordinary_way_and_says_how_to_turn_it_on(self):
        for phrase in ("status", "status of all my agents"):
            ctx = self.prompt(phrase)
            self.assertIsNone(self.last_hook.get("decision"))
            self.assertIn("Answer the ordinary way", ctx)
            self.assertTrue(ctx.rstrip().endswith(ds.TURN_ON_LINE))
        self.assertEqual(self.calls, [])

    def test_status_set_on_windows_says_one_line_and_keeps_no_copy(self):
        """Review 1 LOW 4: no file locking (Windows): no crash, no local copy, one plain line."""
        saved = ds.fcntl
        ds.fcntl = None
        try:
            rc, out, err = self.run_cmd(ds.cmd_status_set, ["-"], stdin=json.dumps({"doing": "Working."}))
        finally:
            ds.fcntl = saved
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        self.assertIn("nothing was saved", out)
        self.assertIsNone(ds.read_status_cache(WB))
        self.assertIsNone(ds.read_json(ds._outbox_path("web-app"), None))
        self.assertEqual(self.spawned, [])

    def test_nothing_is_sent(self):
        rc, out, err = self.run_cmd(ds.cmd_status_set, ["-"], stdin=json.dumps({"doing": "Working."}))
        self.assertEqual(rc, 0, err)
        self.assertIn("kept on this machine only", out)
        self.assertEqual([a for a in self.spawned if "status-send" in a], [])
        self.assertEqual(self.calls, [])


class InstallQuestion(Base):
    def setUp(self):
        super().setUp()
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        self.calls.clear()

    def test_the_first_list_asks_once(self):
        text = self.prompt("status of all my agents")
        self.assertIn(ds.INSTALL_HEAD, text)
        self.assertTrue(text.rstrip().endswith("Did we miss any of your agents, or is there one you don't want on datastash?"))
        self.assertNotIn(ds.INSTALL_QUESTION, self.prompt("status of all my agents"))

    def test_leave_off_is_saved_by_the_hook_before_the_ok(self):
        """Review 1 HIGH (PM ruling): Claude Code writes no transcript entry for a blocked prompt, so the hook saves the
        leave-off itself, and shows the OK only once it is saved. No checker is started."""
        self.assertTrue(ds.consent_for(self.folder))
        n = len(self.spawned)
        text = self.prompt("leave web-app off")
        self.assertEqual(self.last_hook["decision"], "block")
        self.assertIn('"web-app" is off datastash on this computer', text)  # XLI-328: full OFF
        self.assertFalse(ds.consent_for(self.folder))
        self.assertEqual(self.spawned[n:], [])
        self.assertEqual([c for c in self.calls if c[1] != ds.MCP_PATH], [])  # only the agent's own switch-off
        # the refresher, the sender and the Stop reminder all read consent_for: off now (and no agent works there)
        self.run_cmd(ds.cmd_status_set, ["-"], stdin=json.dumps({"doing": "Working."}))
        self.assertEqual([a for a in self.spawned if "status-send" in a], [])
        self.assertIsNone(self.prompt("leave nobody-here off"))

    def test_leave_off_says_ok_only_when_saved(self):
        saved = ds.record_left_off
        def fail(_folders, *_a):
            raise OSError("read-only")
        ds.record_left_off = fail
        try:
            text = self.prompt("leave web-app off")
        finally:
            ds.record_left_off = saved
        self.assertNotIn("OK", text)
        self.assertIn("could not be left off", text)
        self.assertTrue(ds.consent_for(self.folder))

    def test_private_is_coming(self):
        text = self.prompt("keep web-app private")
        self.assertTrue(text.startswith("Private mode is coming."))
        self.assertTrue(ds.consent_for(self.folder))


class TimedRead(Base):
    """The card's timed read: the real hook process answers "status of all my agents" from a big local copy (30 agents
    x 5 projects) in under 2 seconds, making no request at all (a request would hang 10 s and be recorded)."""
    def test_under_two_seconds_with_no_request(self):
        rows = [row(f"agent-{a:02d}", f"Agent {a} works on project {p}; tests pass.", ago=60 + a, project=f"proj-{p}",
                    asks=[{"id": f"q{a}{p}", "text": f"Merge part {p}?"}]) for a in range(30) for p in range(5)]
        ds.save_status_cache(WB, [ds.norm_status_row(r) for r in rows], time.time() - 20, merge=False)
        ds.write_private(ds.INSTALL_ASKED_PATH, "{}")
        record = os.path.join(harness.ROOT, f"requests-{uuid.uuid4().hex}.txt")
        wrapper = os.path.join(harness.ROOT, "timed_hook.py")
        with open(wrapper, "w") as f:
            f.write(
                "import importlib.machinery, importlib.util, sys, time\n"
                f"loader = importlib.machinery.SourceFileLoader('ds_timed', {BIN!r})\n"
                "spec = importlib.util.spec_from_loader('ds_timed', loader)\n"
                "ds = importlib.util.module_from_spec(spec)\n"
                "loader.exec_module(ds)\n"
                "def http(method, path, *a, **k):\n"
                f"    open({record!r}, 'a').write(method + ' ' + path + '\\n')\n"
                "    time.sleep(10)\n"
                "    raise OSError('no network in this test')\n"
                "ds.http = http\n"
                "ds.read_key = lambda s: 'ldgr_ag_test'\n"
                "sys.argv = [ds.__file__, 'prompt']\n"
                "sys.exit(ds.main())\n")
        sid = "timed-session-1"
        inp = {"session_id": sid, "prompt": "status of all my agents", "hook_event_name": "UserPromptSubmit",
               "cwd": self.folder, "prompt_id": str(uuid.uuid4()), "transcript_path": self.transcript(sid)}
        env = dict(os.environ, CLAUDE_PROJECT_DIR=self.folder)
        t = time.time()
        r = subprocess.run([sys.executable, "-I", wrapper], input=json.dumps(inp), capture_output=True, text=True,
                           env=env, cwd=self.folder, timeout=30)
        took = time.time() - t
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"].startswith(ds.STATUS_HEAD))
        self.assertIn("agent-00", out["reason"])
        self.assertFalse(os.path.exists(record), "the fast read made a request")
        self.assertLess(took, 2.0)
        print(f"\n  timed read (hook process, 30 agents x 5 projects, local copy): {took * 1000:.0f} ms", file=sys.stderr)


class DetailsBound(Base):
    """XLI-339 (EPM 0.3.0 review LOW-1): "full context of N" is one bounded screen (a display bound, not a data cap:
    nothing stored is cut); when there is more it says how much and how to see the rest ("more of N"), a page at a
    time, from this computer's copy only. The status list stays under 2 seconds."""
    CARDS, RUNNING = 2000, 500

    def setUp(self):
        super().setUp()
        big = row("web-app", "Working through a very long card queue.", ago=30, project="web-app",
                  details="The agent's own words: the queue is long because the import is slow.",
                  context={"branch": "queue", "tests": "all pass"}, asks=[{"id": "qb", "text": "Split the queue?"}],
                  cards=[{"id": f"XLI-{k:04d}", "title": f"card number {k}", "step": "built"} for k in range(self.CARDS)],
                  running=[{"what": f"builder {k}", "on": f"XLI-{k:04d}"} for k in range(self.RUNNING)],
                  waiting_on=[{"what": "the EPM pass", "who": "EPM"}],
                  done_asked=[{"request": "post the summary", "done": True, "proof": "posted"}])
        self.server_rows = [big, dict(EXAMPLE[1])]
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        ds.write_private(ds.INSTALL_ASKED_PATH, "{}")
        self.calls.clear()

    def shown_ids(self, text):
        return re.findall(r"XLI-\d{4} card", text)

    def test_status_list_with_2000_cards_stays_under_2_seconds_and_bounded(self):
        t = time.time()
        text = self.prompt("status of all my agents")
        took = time.time() - t
        self.assertLess(took, 2.0)
        self.assertLessEqual(len(text), 8500, len(text))
        self.assertIn("say 'more on web-app'", text)
        self.assertEqual(self.calls, [])

    def test_full_context_of_n_is_bounded_with_a_more_line(self):
        self.prompt("status of all my agents")
        t = time.time()
        text = self.prompt("full context of 1")
        self.assertLess(time.time() - t, 2.0)
        self.assertEqual(self.last_hook["decision"], "block")
        self.assertTrue(text.startswith("Status of your agents: full context of 1"))
        self.assertLessEqual(len(text), 8500, len(text))
        self.assertIn("The agent's own words", text)  # the agent's own details are on the first screen
        self.assertIn("XLI-0000 card number 0", text)
        m = re.search(r"([\d,]+) more lines? not shown: say 'more of 1'", text)
        self.assertIsNotNone(m, text[-400:])
        shown = len(self.shown_ids(text))
        # every card, every running item, the waiting and done lines: one line each
        self.assertEqual(int(m.group(1).replace(",", "")), self.CARDS + self.RUNNING + 2 - shown)
        # a display bound only: nothing stored is cut
        c = ds.read_status_cache(WB)
        mine = next(r for r in c["agents"] if r["agent"] == "web-app")
        self.assertEqual(len(mine["cards"]), self.CARDS)
        self.assertEqual(len(mine["running"]), self.RUNNING)
        self.assertEqual(self.calls, [])

    def test_more_of_n_pages_through_everything_once(self):
        self.prompt("status of all my agents")
        text = self.prompt("full context of 1")
        seen = self.shown_ids(text)
        running = len(re.findall(r"^   running: builder \d+", text, re.M))
        pages = 1
        while "say 'more of 1'" in text:
            t = time.time()
            text = self.prompt("more of 1")
            self.assertLess(time.time() - t, 2.0)
            self.assertIsNotNone(text, f"page {pages + 1} was not answered")
            self.assertTrue(text.startswith("Status of your agents: more of 1"), text[:200])
            self.assertLessEqual(len(text), 8500, len(text))
            seen += self.shown_ids(text)
            running += len(re.findall(r"^   running: builder \d+", text, re.M))
            pages += 1
            self.assertLess(pages, 200)
        self.assertGreater(pages, 3)  # past the 3-prompt life of the list: the pages carry on
        self.assertEqual(seen, [f"XLI-{k:04d} card" for k in range(self.CARDS)])  # each once, in order
        self.assertEqual(running, self.RUNNING)
        self.assertIn("waiting on: EPM for the EPM pass", text)
        self.assertIn("That is all of 1.", text)
        self.assertEqual(self.calls, [])

    def test_more_of_n_without_an_earlier_page_starts_at_the_top(self):
        self.prompt("status of all my agents")
        text = self.prompt("more of 1")
        self.assertIn("The agent's own words", text)
        self.assertIn("XLI-0000 card number 0", text)
        self.assertIsNone(self.prompt("more of 40"))  # no such number: an ordinary prompt
        self.assertIsNone(self.prompt("more of 1", sid="sess2"))  # another session's list never counts

    def test_full_context_starts_again_at_the_top(self):
        self.prompt("status of all my agents")
        self.prompt("full context of 1")
        self.prompt("more of 1")
        text = self.prompt("full context of 1")
        self.assertIn("XLI-0000 card number 0", text)

    def test_a_short_row_has_no_more_line(self):
        self.prompt("status of all my agents")
        lines = ds.render_status_details(self.folder, ds.lazy_me(), 4, for_person=True)
        text = "\n".join(lines)
        self.assertIn("Delete the old /invoices endpoint", text)
        self.assertNotIn("more of", text)

    def test_the_ai_path_is_bounded_and_framed_with_its_own_more(self):
        self.prompt("status of all my agents")
        rc, out, err = self.run_cmd(ds.cmd_status, ["--details", "1"])
        self.assertEqual(rc, 0, err)
        self.assertLessEqual(len(out), 9000, len(out))
        inner = framed_inner(out)
        self.assertIn("XLI-0000 card number 0", "\n".join(inner))
        self.assertTrue(any("say 'more of 1'" in x for x in inner))
        first = self.shown_ids(out)
        rc, out, err = self.run_cmd(ds.cmd_status, ["--more", "1"])
        self.assertEqual(rc, 0, err)
        self.assertLessEqual(len(out), 9000, len(out))
        nxt = self.shown_ids(out)
        self.assertTrue(nxt)
        self.assertEqual(nxt[0], f"XLI-{len(first):04d} card")

    def test_phrases(self):
        P = ds.status_phrase
        self.assertEqual(P("more of 4"), ("more_of", 4))
        self.assertEqual(P("More of #4."), ("more_of", 4))
        self.assertEqual(P("full context of 4"), ("details", 4))
        self.assertEqual(P("more on 12"), ("details", 12))


class ZzAnswerRoute(unittest.TestCase):
    """XLI-310 item 2: every request any test made to an answer route was the plugin's own typed answer (the login token
    as bearer, a typed proof in the body); never an undo; no answer command for a tool call to run. Self-contained (in
    any order): it runs the answering tests itself first, then checks every request recorded so far."""
    def test_only_typed_answers_went_to_the_answer_route(self):
        start = len(ALL_CALLS)
        loader = unittest.TestLoader()
        suite = unittest.TestSuite(loader.loadTestsFromTestCase(c) for c in (TypedAnswer, DoN, FastAnswer))
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual((result.failures, result.errors), ([], []))
        self.assertTrue([c for c in ALL_CALLS[start:] if "/answer" in c[1]])  # this run made answers through the recorder
        self.assertEqual([c for c in ALL_CALLS if "/undo" in c[1]], [])
        answers = [c for c in ALL_CALLS if "/answer" in c[1]]
        self.assertTrue(answers)
        for c in answers:
            self.assertEqual(len(c), 4, c)  # recorded by the typed-answer tests' own route
            method, path, key, body = c
            self.assertEqual((method, path, key), ("POST", ds.PERSON_ANSWER_PATH, "dsat_person"))
            self.assertEqual(set(body["typed_proof"]), {"v", "prompt_id", "typed_sha256", "typed_at", "iat", "mac"})

    def test_no_answer_command_in_the_plugin(self):
        with open(BIN) as f:
            code = f.read()
        for word in ("/answer/undo", '"answer": cmd_answer', "status-answer-send", "def undo_answer"):
            self.assertNotIn(word, code)
        r = subprocess.run([sys.executable, "-I", BIN, "answer", "3"], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 1)  # unknown command: the help text, nothing else

    def test_the_skill_names_the_servers_marker_and_the_typed_phrases(self):
        with open(os.path.join(HERE, "..", "plugins", "datastash", "skills", "datastash-agent", "SKILL.md")) as f:
            skill = f.read()
        self.assertIn("status_answer", skill)
        self.assertNotIn("person_answer", skill)
        self.assertNotIn("undo N", skill)
        self.assertNotIn("answering asks from here is coming", skill)
        for phrase in ('"do N"', '"no to N"', '"N: <text>"'):
            self.assertIn(phrase, skill)
        self.assertIn("never call `ledger_agent_status` with action `answer`", skill)


if __name__ == "__main__":
    unittest.main(verbosity=1)
