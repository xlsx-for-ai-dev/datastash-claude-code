#!/usr/bin/env python3
"""Offline tests for XLI-325 (plugin 0.9.3): "your own week in numbers", from this computer's own Claude Code history.
- Plain local code that never sends anything. The first time, `datastash week` asks before reading any history, with
  the card's words; `datastash week yes` / `no` records the person's answer (only the answer is kept).
- It reads timestamps, tool-call shapes and message gaps only, never text, and keeps only counts.
- ONE line, measured the way the 11-22 measured method did: hours agents waited on the person, time messages between
  agents sat unread, minutes spent on repeated lookups. Tiny numbers are called tiny; nothing is rounded up.
The history here is synthetic (a throwaway Claude Code folder, enforced by test/harness.py).
Run: python3 -I test/test_week.py"""
import datetime
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import ds, BIN  # noqa: E402  (the same harness and plugin copy)

SECRET = "ZEBRA-PLANTED-WORD"
NOW = datetime.datetime(2026, 11, 23, 12, 0, 0, tzinfo=datetime.timezone.utc)
T0 = NOW - datetime.timedelta(days=2)


def at(base, **kw):
    return (base + datetime.timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def user(ts, content, **kw):
    r = {"type": "user", "timestamp": ts, "isSidechain": False, "entrypoint": kw.pop("entrypoint", "cli"),
         "message": {"role": "user", "content": content}}
    r.update(kw)
    return r


def typed(ts, text, **kw):
    return user(ts, f"{text} {SECRET}", origin={"kind": "human"}, promptSource="typed", **kw)


def said(ts, text="done", entrypoint="cli"):
    return {"type": "assistant", "timestamp": ts, "isSidechain": False, "entrypoint": entrypoint,
            "message": {"role": "assistant", "content": [{"type": "text", "text": f"{text} {SECRET}"}]}}


def bash(ts, tid, cmd, entrypoint="cli", background=False):
    inp = {"command": cmd, "description": SECRET}
    if background:
        inp["run_in_background"] = True
    return {"type": "assistant", "timestamp": ts, "isSidechain": False, "entrypoint": entrypoint,
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": "Bash", "input": inp}]}}


def result(ts, tid, entrypoint="cli"):
    return user(ts, [{"type": "tool_result", "tool_use_id": tid, "content": f"out {SECRET}"}], entrypoint=entrypoint)


def peer(ts, body, **origin):
    o = {"kind": "peer", "from": "builder"}
    o.update(origin)
    return user(ts, f'<agent-message from="builder">{body} {SECRET}</agent-message>', origin=o, isMeta=True)


def enqueue(ts, body):
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": ts, "sessionId": "p1",
            "content": f'<agent-message from="builder">{body} {SECRET}</agent-message>'}


def queued_peer(queued_ts, delivered_ts, body):
    return {"type": "attachment", "timestamp": delivered_ts, "isSidechain": False, "entrypoint": "cli",
            "attachment": {"type": "queued_command", "prompt": f"{body} {SECRET}", "commandMode": "prompt",
                           "origin": {"kind": "peer", "from": "reviewer"}, "timestamp": queued_ts, "isMeta": True}}


def person_session():
    """The person's own session (entrypoint cli)."""
    return [
        typed(at(T0), "start"),
        said(at(T0, seconds=10)),
        typed(at(T0, minutes=30), "next"),               # waited on the person: 29 min 50 s
        said(at(T0, minutes=30, seconds=5)),
        typed(at(T0, minutes=31), "quick"),             # 55 s: reaction time, not counted
        bash(at(T0, minutes=31, seconds=5), "b1", "gh pr view 5 --repo acme/web"),
        typed(at(T0, minutes=50), "typed while a tool ran"),  # a tool still open: the agent had not ended its turn
        result(at(T0, minutes=50, seconds=1), "b1"),
        said(at(T0, minutes=50, seconds=2)),
        # messages from other agents
        enqueue(at(T0, hours=2), "review is done"),
        peer(at(T0, hours=2, minutes=20), "review is done"),  # sat 20 min until delivered ...
        said(at(T0, hours=2, minutes=20, seconds=5)),        # ... and 5 s more until the agent acted: 20 min 5 s
        queued_peer(at(T0, hours=3), at(T0, hours=3, minutes=2), "a question"),  # 2 min queued ...
        said(at(T0, hours=3, minutes=2, seconds=10)),         # ... + 10 s: 2 min 10 s
        peer(at(T0, hours=4), "final report", handback=True, senderTaskId="t1"),  # a subagent's handback: not a message
        said(at(T0, hours=4, minutes=30)),
        # repeated lookups (asked again within 10 minutes)
        bash(at(T0, hours=5), "l1", "gh pr view 5"),
        result(at(T0, hours=5, seconds=40), "l1"),
        bash(at(T0, hours=5, minutes=5), "l2", "gh pr view 5"),        # repeat: 90 s
        result(at(T0, hours=5, minutes=6, seconds=30), "l2"),
        bash(at(T0, hours=5, minutes=6), "l3", "gh pr view 6"),        # another PR: not a repeat
        result(at(T0, hours=5, minutes=7), "l3"),
        bash(at(T0, hours=5, minutes=40), "l4", "gh pr view 5"),       # 32 min after the last ask (the subagent's): not a repeat
        result(at(T0, hours=5, minutes=41), "l4"),
        bash(at(T0, hours=5, minutes=42), "l5", "gh pr view 5", background=True),  # repeat, background: not timed
        result(at(T0, hours=5, minutes=42, seconds=1), "l5"),
        bash(at(T0, hours=5, minutes=43), "l6", "git status"),           # not a lookup
        result(at(T0, hours=5, minutes=50), "l6"),
        said(at(T0, hours=5, minutes=51)),
        typed(at(T0, hours=6), "back"),                  # waited 9 min
    ]


def builder_session():
    """A builder started by another agent (claude -p): no person in it, so no waiting on the person."""
    e = "sdk-cli"
    return [
        user(at(T0, hours=1), f"brief {SECRET}", entrypoint=e, origin={"kind": "human"}),
        said(at(T0, hours=1, seconds=5), entrypoint=e),
        user(at(T0, hours=2), f"more {SECRET}", entrypoint=e, origin={"kind": "human"}),
    ]


def subagent_session():
    """A subagent's transcript: its lookup repeats the person session's one from another session."""
    return [
        bash(at(T0, hours=5, minutes=8), "s1", "gh pr view 5", entrypoint="sdk-cli"),  # 3 min after l2: repeat, 60 s
        result(at(T0, hours=5, minutes=9), "s1", entrypoint="sdk-cli"),
    ]


def old_session():
    """Older than the week: never counted."""
    old = NOW - datetime.timedelta(days=9)
    return [typed(at(old), "old"), said(at(old, seconds=1)), typed(at(old, hours=3), "old again")]


def write(path, recs, extra_lines=()):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
        for line in extra_lines:
            f.write(line + "\n")


def make_history(root):
    proj = os.path.join(root, "projects", "-home-me-web")
    write(os.path.join(proj, "p1.jsonl"), person_session(), extra_lines=("not json " + SECRET, ""))
    write(os.path.join(proj, "b1.jsonl"), builder_session())
    write(os.path.join(proj, "p1", "subagents", "agent-a1.jsonl"), subagent_session())
    write(os.path.join(proj, "old.jsonl"), old_session())
    old = os.path.join(proj, "old.jsonl")
    t = (NOW - datetime.timedelta(days=9)).timestamp()
    os.utime(old, (t, t))


EXPECTED = ("Your week in numbers (measured on this computer from its Claude Code history, last 7 days; nothing was "
            "sent): your agents waited on you 38 min (2 waits); messages between agents sat unread 22 min "
            "(2 messages); repeated lookups took 2 min (3 repeats).")


class Week(unittest.TestCase):
    def setUp(self):
        # A Claude Code folder of this test's own: the shared one under the throwaway HOME holds other test files'
        # sessions when the whole suite runs in one process.
        self.claude = tempfile.mkdtemp(prefix="week-claude-", dir=ds.HOME)
        self.saved = {k: getattr(ds, k) for k in ("CLAUDE_DIR", "http", "_week_now")}
        ds.CLAUDE_DIR = self.claude
        make_history(self.claude)
        self.http_calls = []

        def no_http(*a, **k):
            self.http_calls.append(a)
            raise AssertionError("week must never send anything")
        ds.http = no_http
        ds._week_now = lambda: NOW.timestamp()
        try:
            os.replace(ds.WEEK_PATH, ds.WEEK_PATH + ".old")
        except OSError:
            pass

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(ds, k, v)
        self.assertEqual(self.http_calls, [])

    def run_week(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ds.cmd_week(list(args))
        return code, buf.getvalue()

    def test_measured_numbers_on_the_test_history(self):
        n = ds.week_numbers(self.claude, NOW.timestamp())
        self.assertEqual(n["waits"], 2)
        self.assertAlmostEqual(n["wait_s"], 29 * 60 + 50 + 9 * 60, places=3)
        self.assertEqual(n["messages"], 2)
        self.assertAlmostEqual(n["unread_s"], 20 * 60 + 5 + 2 * 60 + 10, places=3)
        self.assertEqual(n["repeats"], 3)  # the background repeat is counted, never timed
        self.assertEqual(n["timed_repeats"], 2)
        self.assertAlmostEqual(n["lookup_s"], 90 + 60, places=3)
        self.assertEqual(ds.week_line(n), EXPECTED)

    def test_asks_first_and_reads_nothing_until_yes(self):
        read = []
        real = ds.week_numbers
        ds.week_numbers = lambda *a, **k: read.append(a) or real(*a, **k)
        try:
            code, out = self.run_week()
            self.assertEqual(code, 0)
            self.assertIn("Want to see what last week cost your agents? I'll read this computer's Claude Code "
                          "history; nothing leaves it.", out)
            self.assertIn("datastash week yes", out)
            self.assertEqual(read, [])
            code, out = self.run_week("no")
            self.assertEqual(read, [])
            self.assertIn("won't read", out)
            code, out = self.run_week()
            self.assertEqual(read, [])
            self.assertIn("datastash week yes", out)
            code, out = self.run_week("yes")
            self.assertEqual(code, 0)
            self.assertEqual(len(read), 1)
            self.assertEqual(out.strip().count("\n"), 0, "one line")
            self.assertTrue(out.startswith("Your week in numbers (measured"))
            code, out2 = self.run_week()
            self.assertEqual(out2, out)
        finally:
            ds.week_numbers = real

    def test_keeps_no_text_only_the_answer(self):
        self.run_week("yes")
        with open(ds.WEEK_PATH) as f:
            kept = json.load(f)
        self.assertEqual(set(kept), {"answer", "at"})
        self.assertEqual(kept["answer"], "yes")
        n = ds.week_numbers(self.claude, NOW.timestamp())
        blob = json.dumps(n) + ds.week_line(n)
        for word in (SECRET, "gh pr", "acme", "review is done", "builder"):
            self.assertNotIn(word, blob)
        self.assertTrue(all(isinstance(v, (int, float)) for v in n.values()), n)

    def test_tiny_and_empty_numbers_are_said_plainly(self):
        n = {"waits": 1, "wait_s": 30.0, "messages": 0, "unread_s": 0.0, "repeats": 1, "timed_repeats": 1,
             "lookup_s": 4.0, "days": 7}
        line = ds.week_line(n)
        self.assertIn("your agents waited on you under a minute (tiny)", line)
        self.assertIn("no messages between agents", line)
        self.assertIn("repeated lookups took under a minute (tiny)", line)
        n.update(wait_s=3599.0, waits=3, lookup_s=119.0)
        line = ds.week_line(n)
        self.assertIn("59 min (3 waits)", line)  # never rounded up
        self.assertIn("1 min (1 repeat)", line)
        n.update(wait_s=3 * 3600 + 359.0)
        self.assertIn("3.0 h (3 waits)", ds.week_line(n))

    def test_no_history_says_so(self):
        empty = os.path.join(ds.HOME, "empty-claude")
        os.makedirs(empty, exist_ok=True)
        n = ds.week_numbers(empty, NOW.timestamp())
        self.assertIn("No Claude Code history on this computer in the last 7 days", ds.week_line(n))

    def test_short_history_names_its_days(self):
        n = ds.week_numbers(self.claude, (T0 + datetime.timedelta(hours=7)).timestamp())
        self.assertIn("last 7 days", ds.week_line(n))
        only = os.path.join(ds.HOME, "short-claude")
        write(os.path.join(only, "projects", "x", "s.jsonl"), person_session())
        n = ds.week_numbers(only, (T0 + datetime.timedelta(days=2, hours=7)).timestamp())
        self.assertIn("last 3 days", ds.week_line(n))

    def test_the_command_runs_with_no_network(self):
        env = dict(os.environ)
        r = subprocess.run([sys.executable, "-I", BIN, "week"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Want to see what last week cost your agents?", r.stdout)


if __name__ == "__main__":
    unittest.main()
