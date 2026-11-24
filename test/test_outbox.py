#!/usr/bin/env python3
"""Offline tests for XLI-408 (plugin 1.0.13): the offline outbox. Owner 11-24: "we need this to be seamless. if the server
goes down or can't be reached stuff needs to get kept at the plugin side and sent once its up."
- While datastash can't be reached (no connection, a timeout, a name that does not resolve, a 5xx), a write through
  `datastash api POST` (and the person's send or reply) is kept on this computer, in order, in the plugin's guarded
  folder, and the caller is told "saved, will send when the server is back" (exit 0, not an error).
- Once datastash answers, the kept writes go out in order, exactly once each: at the next call, at session start (a
  background send) and from the listener's background thread; one sender at a time; each carries one idempotency_key
  (handoff_id on a hand-off), so a resend after a crash mid-send makes no second copy.
- Reads are never kept: they say plainly that datastash can't be reached and to try again shortly.
- No caps: any number of writes is kept.
A fake datastash here applies each write once per idempotency_key, the way x-live does (#502, #506). No network, no key;
under test/harness.py.
Run: python3 -I test/test_outbox.py"""
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import ds, TMP  # noqa: E402  (the same harness and plugin copy)
import harness  # noqa: E402

WB = "0b6a3f9e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
KEY = "ldgr_ag_testkeytestkeytestkey"
SLUG = "outbox-agent"


class FakeDatastash:
    """Applies each write once per (path, idempotency_key), as x-live does; a repeat answers the first (replayed).
    down: None (up), or an exception to raise for every call (not reached). after_apply: raise this once, after the
    write was applied (the answer never arrived: a crash or a dropped connection mid-send)."""

    def __init__(self):
        self.down = None
        self.after_apply = None
        self.applied = []  # (path, body) in the order datastash applied them
        self.calls = []
        self.seen = {}
        self.taken = set()  # keys another caller already used: refused 400, as x-live's REPLAY_KEY_TAKEN
        self.status_for = {}  # path -> an HTTP status to answer instead (a 4xx refusal)
        self.lock = threading.Lock()
        self.delay = 0.0

    def http(self, method, path, key=None, body=None, form=None, timeout=40, accept="application/json"):
        with self.lock:
            self.calls.append((method, path, json.loads(json.dumps(body))))
        if self.delay:
            time.sleep(self.delay)
        if self.down is not None:
            raise self.down
        if method == "GET":
            return 200, "application/json", json.dumps({"messages": []})
        if path in self.status_for:
            raise ds.ApiError(self.status_for[path], {"code": "bad_request", "message": "Send tab: the tab id or name."})
        rk = (body or {}).get("idempotency_key") or (body or {}).get("handoff_id")
        with self.lock:
            if rk in self.taken:
                raise ds.ApiError(400, {"code": "bad_request", "message": "That idempotency_key was already used by another caller in this account; send a new one. Nothing changed."})
            if rk is not None and (path, rk) in self.seen:
                return 200, "application/json", json.dumps(dict(self.seen[(path, rk)], replayed=True))
            self.applied.append((path, body))
            answer = {"id": len(self.applied)}
            if rk is not None:
                self.seen[(path, rk)] = answer
        if self.after_apply is not None:
            e, self.after_apply = self.after_apply, None
            raise e
        return 200, "application/json", json.dumps(answer)

    def texts(self):
        return [b.get("message") or json.dumps(b.get("row") or b.get("result") or b) for _p, b in self.applied]


class Base(unittest.TestCase):
    def setUp(self):
        self.folder = os.path.realpath(harness.tmpdir(self, prefix="proj-", dir=TMP))
        ds.ensure_dirs()
        ds.write_private(ds.conf_path(SLUG), json.dumps({"name": SLUG, "workbook_id": WB, "projects": [self.folder]}))
        self.fake = FakeDatastash()
        self.spawned = []
        self.saved = {}
        for name, new in (("http", self.fake.http), ("read_key", lambda s: KEY),
                          ("spawn_detached", lambda argv, cwd: self.spawned.append((argv, cwd))),
                          ("auto_setup_check", lambda *a, **k: None)):
            self.saved[name] = getattr(ds, name)
            setattr(ds, name, new)
        self.env = {k: os.environ.get(k) for k in ("CLAUDE_PROJECT_DIR",)}
        os.environ["CLAUDE_PROJECT_DIR"] = self.folder
        self.addCleanup(self.clear_outbox)

    def clear_outbox(self):
        d = ds.outbox_dir(SLUG)
        for n in (os.listdir(d) if os.path.isdir(d) else []):
            os.unlink(os.path.join(d, n))  # test code clearing its own throwaway folder
        ds._drop(ds._outbox_refused_path(SLUG))

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(ds, k, v)
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def api(self, method, path, body=None):
        args = [method, path] + ([json.dumps(body)] if body is not None else [])
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            try:
                code = ds.cmd_api(args)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def send(self, text, to="Friend"):
        return self.api("POST", "/api/v1/agent/inbox", {"workbook_id": WB, "to": to, "message": text})

    def kept(self):
        return len(ds.outbox_entries(SLUG))


class DownThenUp(Base):
    def test_writes_are_kept_in_order_then_each_sent_exactly_once_in_order(self):
        self.fake.down = ds.urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        for i in range(5):
            code, out, err = self.send(f"m{i}")
            self.assertEqual(code, 0, err)
            self.assertIn(ds.OUTBOX_SAVED, out)
            self.assertIn("ConnectionRefusedError", out)
        self.assertEqual(self.kept(), 5)
        self.assertEqual(self.fake.applied, [])
        self.fake.down = None
        code, out, err = self.send("m5")  # the next call: the line goes out first, then this one
        self.assertEqual(code, 0, err)
        self.assertIn("<<datastash-data", out)  # its own answer, framed as data
        self.assertNotIn(ds.OUTBOX_SAVED, out)
        self.assertEqual(self.fake.texts(), [f"m{i}" for i in range(6)])
        self.assertEqual(self.kept(), 0)
        keys = [b["idempotency_key"] for _p, b in self.fake.applied]
        self.assertEqual(len(set(keys)), 6)
        self.assertTrue(all(k.startswith("dso_") for k in keys))
        self.assertEqual(ds.outbox_send(SLUG)["sent"], 0)  # nothing left, nothing sent twice
        self.assertEqual(len(self.fake.applied), 6)

    def test_every_way_of_not_reaching_datastash_is_kept(self):
        import socket
        for e in (ds.urllib.error.URLError(socket.gaierror(-2, "Name or service not known")), TimeoutError("timed out"),
                  socket.timeout("timed out"), ConnectionResetError(104, "reset"), ds.httpclient.RemoteDisconnected("closed"),
                  ds.ApiError(502, {}), ds.ApiError(503, {}), ds.ApiError(500, {"error": "internal"})):
            self.fake.down = e
            code, out, err = self.send(f"via {type(e).__name__}")
            self.assertEqual(code, 0, err)
            self.assertIn(ds.OUTBOX_SAVED, out, type(e).__name__)
        self.assertEqual(self.kept(), 8)
        self.fake.down = None
        self.assertEqual(ds.outbox_send(SLUG)["sent"], 8)
        self.assertEqual(len(self.fake.applied), 8)

    def test_a_write_datastash_refuses_is_answered_as_before_never_kept(self):
        self.fake.status_for["/api/v1/agent/rows"] = 400
        code, out, _err = self.api("POST", "/api/v1/agent/rows", {"workbook_id": WB, "tab": "x", "row": {"a": 1}})
        self.assertEqual(code, 1)
        self.assertIn("HTTP 400", out)
        self.assertEqual(self.kept(), 0)
        self.fake.down = ds.ApiError(429, {"error": "busy"})
        code, out, _err = self.send("busy")
        self.assertEqual((code, self.kept()), (1, 0))  # a busy answer is datastash answering: never kept (its brake holds)

    def test_no_caps_a_large_outbox_stays_correct(self):
        self.fake.down = ds.urllib.error.URLError("no route")
        for i in range(300):
            ds.outbox_put(SLUG, "POST", "/api/v1/agent/inbox", ds.replay_safe("/api/v1/agent/inbox", {"workbook_id": WB, "message": f"n{i}"}), "test")
        self.assertEqual(self.kept(), 300)
        self.fake.down = None
        r = ds.outbox_send(SLUG)
        self.assertEqual((r["sent"], r["left"]), (300, 0))
        self.assertEqual(self.fake.texts(), [f"n{i}" for i in range(300)])

    def test_a_new_write_while_kept_ones_wait_joins_the_end_of_the_line(self):
        self.fake.down = ds.urllib.error.URLError("down")
        self.send("first")
        code, out, _err = self.send("second")
        self.assertIn(ds.OUTBOX_SAVED, out)
        self.assertIn("1 earlier write", out)
        self.assertEqual(self.kept(), 2)

    def test_the_line_stops_at_a_write_that_cant_go_yet_and_keeps_its_order(self):
        self.fake.down = ds.urllib.error.URLError("down")
        for t in ("a", "b", "c"):
            self.send(t)
        self.fake.down = None
        real = self.fake.http
        n = {"c": 0}

        def flaky(method, path, **kw):
            n["c"] += 1
            if n["c"] == 2:
                raise ds.ApiError(503, {})
            return real(method, path, **kw)
        ds.http = flaky
        r = ds.outbox_send(SLUG)
        self.assertEqual((r["sent"], r["left"]), (1, 2))
        ds.http = real
        ds.outbox_send(SLUG)
        self.assertEqual(self.fake.texts(), ["a", "b", "c"])

    def test_a_key_or_busy_answer_on_resend_keeps_the_write_in_its_place(self):
        self.fake.down = ds.urllib.error.URLError("down")
        self.send("a")
        for st in (401, 403, 429):
            self.fake.down = ds.ApiError(st, {})
            self.assertEqual(ds.outbox_send(SLUG)["left"], 1, st)
        self.fake.down = None
        ds.outbox_send(SLUG)
        self.assertEqual(self.fake.texts(), ["a"])


class Replay(Base):
    def test_a_crash_mid_send_resends_with_the_same_key_and_makes_no_second_copy(self):
        self.fake.down = ds.urllib.error.URLError("down")
        self.send("only once")
        self.send("after it")
        self.fake.down = None
        self.fake.calls = []
        self.fake.after_apply = ConnectionResetError(104, "reset after datastash applied it")
        r = ds.outbox_send(SLUG)
        self.assertEqual((r["sent"], r["left"]), (0, 2))  # the answer never came: kept in its place
        self.assertEqual(len(self.fake.applied), 1)
        r = ds.outbox_send(SLUG)
        self.assertEqual((r["sent"], r["left"]), (2, 0))
        self.assertEqual(self.fake.texts(), ["only once", "after it"])
        first = [c for c in self.fake.calls if c[2] and c[2].get("message") == "only once"]
        self.assertEqual(len(first), 2)
        self.assertEqual(first[0][2]["idempotency_key"], first[1][2]["idempotency_key"])

    def test_a_process_killed_after_the_answer_before_the_line_moved_on_sends_once(self):
        self.fake.down = ds.urllib.error.URLError("down")
        self.send("killed mid-send")
        self.fake.down = None
        real_drop = ds._drop

        def killed(path):
            raise SystemExit("killed")  # the process dies after datastash answered, before the kept file goes
        ds._drop = killed
        try:
            with self.assertRaises(SystemExit):
                ds.outbox_send(SLUG)
        finally:
            ds._drop = real_drop
        self.assertEqual(self.kept(), 1)
        ds.outbox_send(SLUG)
        self.assertEqual(self.kept(), 0)
        self.assertEqual(self.fake.texts(), ["killed mid-send"])  # the resend was answered replayed: one copy

    def test_a_timeout_on_the_first_try_is_kept_with_the_key_already_sent(self):
        self.fake.after_apply = TimeoutError("read timed out")
        code, out, _err = self.send("slow answer")
        self.assertEqual(code, 0)
        self.assertIn(ds.OUTBOX_SAVED, out)
        sent_key = self.fake.calls[-1][2]["idempotency_key"]
        ds.outbox_send(SLUG)
        self.assertEqual(self.fake.texts(), ["slow answer"])
        self.assertEqual(self.fake.calls[-1][2]["idempotency_key"], sent_key)

    def test_a_duplicate_key_datastash_refuses_leaves_the_line_and_is_said_once(self):
        self.fake.down = ds.urllib.error.URLError("down")
        self.send("mine", to="A")
        self.send("next", to="B")
        with open(os.path.join(ds.outbox_dir(SLUG), ds.outbox_entries(SLUG)[0])) as f:
            key = json.load(f)["body"]["idempotency_key"]
        self.fake.taken.add(key)
        self.fake.down = None
        r = ds.outbox_send(SLUG)
        self.assertEqual((r["sent"], r["left"]), (2, 0))  # refused: out of the line; the next one still goes
        self.assertEqual(self.fake.texts(), ["next"])
        code, _out, err = self.send("later")
        self.assertEqual(code, 0)
        self.assertIn("was refused when it was sent: HTTP 400 bad_request", err)
        self.assertIn("/api/v1/agent/inbox", err)
        _c, _o, err = self.send("later again")
        self.assertNotIn("refused", err)  # said once

    def test_retry_keys_go_where_datastash_takes_them(self):
        rs = ds.replay_safe
        for p in ("/api/v1/agent/rows", "/api/v1/agent/inbox", "/api/v1/agent/watches", "/api/v1/agent/result",
                  "/api/v1/person/inbox", "/api/v1/agent/hooks"):
            self.assertTrue(rs(p, {"a": 1})["idempotency_key"].startswith("dso_"), p)
        self.assertEqual(rs("/api/v1/agent/rows", {"idempotency_key": "mine"})["idempotency_key"], "mine")
        self.assertNotIn("idempotency_key", rs("/api/v1/agent/hooks", {"action": "rotate"}))
        self.assertNotIn("idempotency_key", rs("/api/v1/agent/result/reused", {"result_id": "r"}))
        self.assertNotIn("idempotency_key", rs("/api/v1/agent/status", {"doing": "x"}))
        self.assertRegex(rs("/api/v1/agent/handoff", {"task": "t"})["handoff_id"], r"^[0-9a-f-]{36}$")
        self.assertEqual(rs("/api/v1/agent/handoff", {"handoff_id": "h"})["handoff_id"], "h")


class TwoSenders(Base):
    def test_two_sessions_sending_one_outbox_at_once_send_each_write_once(self):
        self.fake.down = ds.urllib.error.URLError("down")
        for i in range(20):
            self.send(f"w{i}")
        self.fake.down = None
        self.fake.calls = []
        self.fake.delay = 0.01
        results = []
        go = threading.Event()

        def sender():
            go.wait()
            results.append(ds.outbox_send(SLUG))
        ts = [threading.Thread(target=sender) for _ in range(4)]
        for t in ts:
            t.start()
        go.set()
        for t in ts:
            t.join(30)
        self.assertEqual(self.fake.texts(), [f"w{i}" for i in range(20)])
        self.assertEqual(sum(r["sent"] for r in results), 20)
        self.assertGreaterEqual(sum(1 for r in results if r["busy"]), 1)
        posts = [c for c in self.fake.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 20)  # no write was even tried twice

    def test_a_sender_in_another_process_holds_the_line(self):
        self.fake.down = ds.urllib.error.URLError("down")
        self.send("held")
        self.fake.down = None
        lock = ds._outbox_lock_path(SLUG, "send")
        holder = subprocess.Popen([sys.executable, "-I", "-c",
                                   "import fcntl,os,sys,time\nfd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT,0o600)\n"
                                   "fcntl.flock(fd,fcntl.LOCK_EX)\nprint('held',flush=True)\ntime.sleep(30)", lock],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            r = ds.outbox_send(SLUG)
            self.assertTrue(r["busy"])
            self.assertEqual((r["sent"], self.fake.applied), (0, []))
            code, out, _err = self.send("behind it")
            self.assertEqual(code, 0)
            self.assertIn("another session is sending the kept writes now", out)
        finally:
            holder.kill()
            holder.wait()
        ds.outbox_send(SLUG)
        self.assertEqual(self.fake.texts(), ["held", "behind it"])


class Background(Base):
    def test_the_listener_sends_the_kept_writes_by_itself(self):
        self.fake.down = ds.urllib.error.URLError("down")
        self.send("from the background")
        self.fake.down = None
        old = ds.OUTBOX_EVERY
        ds.OUTBOX_EVERY = 0.05
        stop = threading.Event()
        live = {"key": None}
        try:
            t = ds.start_outbox_sender({"slug": SLUG}, live, stop)
            time.sleep(0.3)
            self.assertEqual(self.fake.applied, [])  # parked (no live key): nothing goes
            live["key"] = KEY
            deadline = time.time() + 5
            while self.kept() and time.time() < deadline:
                time.sleep(0.05)
        finally:
            stop.set()
            ds.OUTBOX_EVERY = old
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(self.fake.texts(), ["from the background"])

    def test_session_start_kicks_one_background_send_and_says_so(self):
        ds.outbox_kick(SLUG, self.folder)
        self.assertEqual(self.spawned, [])  # nothing kept: nothing started
        self.fake.down = ds.urllib.error.URLError("down")
        self.send("kept")
        ds.outbox_kick(SLUG, self.folder)
        self.assertEqual(len(self.spawned), 1)
        argv = self.spawned[0][0]
        self.assertEqual(argv[-2:], ["outbox-send", SLUG])
        self.fake.down = None
        self.assertEqual(ds.cmd_outbox_send([SLUG]), 0)
        self.assertEqual(self.fake.texts(), ["kept"])
        self.assertEqual(ds.cmd_outbox_send(["../x"]), 1)

    def test_status_counts_the_kept_writes(self):
        self.fake.down = ds.urllib.error.URLError("down")
        self.send("one")
        with redirect_stdout(io.StringIO()) as out:
            ds.cmd_status([])
        self.assertIn("kept writes waiting for datastash: 1", out.getvalue())


class Reads(Base):
    def test_a_read_says_plainly_datastash_cant_be_reached_and_is_never_kept(self):
        for e in (ds.urllib.error.URLError(ConnectionRefusedError(111, "refused")), ds.ApiError(502, {}), TimeoutError("t")):
            self.fake.down = e
            code, out, err = self.api("GET", f"/api/v1/agent/inbox?workbook_id={WB}")
            self.assertEqual(code, 1)
            self.assertIn("datastash can't be reached right now", err)
            self.assertIn("nothing was read. Try again shortly.", err)
            self.assertNotIn(ds.OUTBOX_SAVED, out + err)
        self.assertEqual(self.kept(), 0)
        self.fake.down = None
        code, out, _err = self.api("GET", f"/api/v1/agent/inbox?workbook_id={WB}")
        self.assertEqual(code, 0)
        self.assertIn("<<datastash-data", out)

    def test_sign_in_steps_claims_beats_and_checks_are_never_kept(self):
        self.fake.down = ds.urllib.error.URLError("down")
        for p in ("/api/v1/agent/job", "/api/v1/agent/job/renew", "/api/v1/agent/heartbeat", "/api/v1/agent/rows/touch",
                  "/api/v1/agent/result/check", "/api/v1/agent/hello"):
            code, _out, err = self.api("POST", p, {"workbook_id": WB})
            self.assertEqual(code, 1, p)
            self.assertIn("This call was not kept", err, p)
        self.assertEqual(self.kept(), 0)
        for p in ("/api/v1/agent/rows", "/api/v1/agent/status", "/api/v1/agent/handoff", "/api/v1/agent/inbox/mark",
                  "/api/v1/agent/result", "/api/v1/agent/watches", "/api/v1/agent/tabs"):
            self.assertTrue(ds.outbox_keeps("POST", p), p)
        self.assertFalse(ds.outbox_keeps("GET", "/api/v1/agent/rows"))


class Guarded(Base):
    def test_the_outbox_is_in_the_guarded_folder(self):
        self.fake.down = ds.urllib.error.URLError("down")
        self.send("private words")
        d = ds.outbox_dir(SLUG)
        f = os.path.join(d, ds.outbox_entries(SLUG)[0])
        self.assertEqual(oct(os.stat(d).st_mode & 0o777), "0o700")
        self.assertEqual(oct(os.stat(f).st_mode & 0o777), "0o600")
        for ti in ({"tool_name": "Read", "tool_input": {"file_path": f}},
                   {"tool_name": "Bash", "tool_input": {"command": f"cat {f}"}},
                   {"tool_name": "Bash", "tool_input": {"command": f"ls {d}"}}):
            out = ds.key_path_deny(dict(ti, session_id="sess-outbox", hook_event_name="PreToolUse", cwd=self.folder))
            self.assertEqual(((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision"), "deny", ti)


class Person(Base):
    def test_the_persons_message_is_kept_and_sent_the_same_way(self):
        self.fake.down = ds.urllib.error.URLError("down")
        with redirect_stdout(io.StringIO()) as out:
            code = ds.outbox_call(ds.PERSON_SLUG, KEY, "POST", ds.PERSON_INBOX_PATH, {"workbook_id": WB, "to": "web-app", "message": "from the person"})
        self.assertEqual(code, 0)
        self.assertIn(ds.OUTBOX_SAVED, out.getvalue())
        self.assertEqual(len(ds.outbox_entries(ds.PERSON_SLUG)), 1)
        self.assertEqual(ds.outbox_dir(ds.PERSON_SLUG + "-abc"), ds.outbox_dir(ds.PERSON_SLUG))  # one line for every main session
        self.fake.down = None
        self.assertEqual(ds.outbox_send(ds.PERSON_SLUG + "-abc")["sent"], 1)
        self.assertEqual(self.fake.applied[0][0], ds.PERSON_INBOX_PATH)
        self.assertTrue(self.fake.applied[0][1]["idempotency_key"].startswith("dso_"))


if __name__ == "__main__":
    unittest.main()
