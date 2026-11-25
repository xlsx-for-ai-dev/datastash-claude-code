#!/usr/bin/env python3
"""Offline tests for plugins/datastash/scripts/hours_count.py (plugin 1.0.18): the local "hours saved last week" counter.
Synthetic transcripts in a throwaway HOME; no network, no key. Only numbers may leave the counter.
Run: python3 -m unittest test.test_hours_count   (or: python3 -I test/test_hours_count.py)"""
import calendar
import json
import os
import stat
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "plugins", "datastash", "scripts"))
import hours_count as hc  # noqa: E402

SECRET = "ZEBRA-PLANTED-WORD"
NOW = float(calendar.timegm((2026, 11, 23, 12, 0, 0)))


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def line(**kw):
    return json.dumps(kw, separators=(",", ":"))


def user(t, text, ep="cli"):
    return line(type="user", timestamp=iso(t), entrypoint=ep, sessionId="s1", message={"role": "user", "content": text})


def assistant(t, text=None, tool=None):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "id": tool[0], "name": tool[1], "input": tool[2]})
    return line(type="assistant", timestamp=iso(t), sessionId="s1", message={"role": "assistant", "content": content})


def result(t, tid):
    return line(type="user", timestamp=iso(t), sessionId="s1",
                message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]})


def enqueue(t, c):
    return line(type="queue-operation", operation="enqueue", timestamp=iso(t), sessionId="s1", content=c)


def deliver(t, c):
    return line(type="queue-operation", operation="remove", reason="delivered_to_agent", timestamp=iso(t),
                sessionId="s1", content=c)


NOTICE = '<task-notification><task-id>b1</task-id><summary>Background command "job" completed</summary></task-notification>'


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.realpath(self._tmp.name)

    def write(self, cfg, name, lines, age=0, sub=False):
        d = os.path.join(cfg, "projects", "proj")
        if sub:
            d = os.path.join(d, "sess", "subagents")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.utime(p, (NOW - age, NOW - age))
        return p

    def cfg(self, name=".claude"):
        return os.path.join(self.home, name)

    def est(self, dirs, **kw):
        kw.setdefault("workers", 1)
        return hc.estimate(dirs, now=NOW, **kw)

    def missed_event_lines(self, base, ask=False):
        say = "Should I deploy now? Let me know." if ask else "Started the job, it is running."
        return [user(base, "go " + SECRET), assistant(base + 5, say),
                enqueue(base + 10, NOTICE), deliver(base + 1210, NOTICE), assistant(base + 1215, "Job finished.")]


class Tests(Base):
    def test_no_transcripts(self):
        os.makedirs(self.cfg())
        self.assertIsNone(self.est([self.cfg()]))
        self.assertIsNone(self.est([]))

    def test_one_config_folder_quiet_week(self):
        self.write(self.cfg(), "a.jsonl", [user(NOW - 3600, "hi"), assistant(NOW - 3590, "Hello.")])
        r = self.est([self.cfg()])
        self.assertEqual((r["hours"], r["plans"], r["agents"], r["subagents"], r["files"], r["days"]),
                         (0.0, 1, 1, 0, 1, 7))
        self.assertGreater(r["bytes"], 0)

    def test_several_config_folders(self):
        env_dir = os.path.join(self.home, "elsewhere")
        harness = os.path.join(self.home, ".claude-plan2")
        for c in (self.cfg(), env_dir, harness):
            self.write(c, "a.jsonl", [user(NOW - 600, "hi"), assistant(NOW - 590, "Hello.")])
        # a harness subagent transcript
        self.write(harness, "agent-1.jsonl", [assistant(NOW - 500, "ok")], sub=True)
        dirs = hc.find_config_dirs(home=self.home, env={"CLAUDE_CONFIG_DIR": env_dir})
        self.assertEqual(sorted(os.path.realpath(d) for d in dirs), sorted([self.cfg(), env_dir, harness]))
        again = hc.find_config_dirs(home=self.home, env={"CLAUDE_CONFIG_DIR": env_dir + os.pathsep + self.cfg()})
        self.assertEqual(len(again), 3)  # deduped by real path
        r = self.est(dirs, workers=2)
        self.assertEqual((r["plans"], r["agents"], r["subagents"], r["files"]), (3, 3, 1, 4))

    def test_old_files_skipped(self):
        self.write(self.cfg(), "old.jsonl", self.missed_event_lines(NOW - 20 * 86400), age=20 * 86400)
        self.assertIsNone(self.est([self.cfg()]))
        # fresh file mtime but only old records inside: nothing in the window either
        self.write(self.cfg(), "stale.jsonl", self.missed_event_lines(NOW - 20 * 86400))
        self.assertIsNone(self.est([self.cfg()]))
        self.write(self.cfg(), "new.jsonl", [user(NOW - 100, "hi"), assistant(NOW - 90, "Hello.")])
        self.assertEqual(self.est([self.cfg()])["files"], 1)

    def test_missed_event_counted(self):
        self.write(self.cfg(), "a.jsonl", self.missed_event_lines(NOW - 86400))
        r = self.est([self.cfg()])
        self.assertEqual(r["hours"], 0.3)  # enqueue to action 1205 s, over the 5 min floor

    def test_short_wait_not_counted(self):
        base = NOW - 86400
        lines = [user(base, "go"), assistant(base + 5, "Started the job, it is running."), enqueue(base + 10, NOTICE),
                 deliver(base + 60, NOTICE), assistant(base + 65, "Job finished.")]
        self.write(self.cfg(), "a.jsonl", lines)
        self.assertEqual(self.est([self.cfg()])["hours"], 0.0)

    def test_waiting_on_the_person_not_counted(self):
        self.write(self.cfg(), "a.jsonl", self.missed_event_lines(NOW - 86400, ask=True))
        self.assertEqual(self.est([self.cfg()])["hours"], 0.0)

    def test_repeated_lookup_counted(self):
        base, lines = NOW - 86400, [user(NOW - 86500, "check")]
        for i in range(5):  # same question every 400 s, 250 s each: four repeats = 1000 s
            t = base + i * 400
            lines += [assistant(t, tool=("t%d" % i, "Bash", {"command": "gh pr view 12 --json state"})),
                      result(t + 250, "t%d" % i)]
        self.write(self.cfg(), "a.jsonl", lines)
        self.assertEqual(self.est([self.cfg()])["hours"], 0.3)

    def test_lookups_spaced_apart_or_local_not_counted(self):
        base, lines = NOW - 86400, []
        for i in range(4):
            t = base + i * 2000  # more than 10 min apart
            lines += [assistant(t, tool=("g%d" % i, "Bash", {"command": "gh pr view 12"})), result(t + 250, "g%d" % i)]
        for i in range(4):  # local reads, repeated
            t = base + 9000 + i * 400
            lines += [assistant(t, tool=("l%d" % i, "Bash", {"command": "grep -rn foo src && ls"})), result(t + 250, "l%d" % i)]
        self.write(self.cfg(), "a.jsonl", lines)
        self.assertEqual(self.est([self.cfg()])["hours"], 0.0)

    def test_output_is_numbers_only(self):
        lines = self.missed_event_lines(NOW - 86400)
        lines.append(assistant(NOW - 80000, tool=("x1", "Bash", {"command": "curl https://%s.example.com/a/b" % SECRET.lower()})))
        self.write(self.cfg(), "a-" + SECRET + ".jsonl", lines)
        r = self.est([self.cfg()])
        self.assertEqual(set(r), {"hours", "plans", "agents", "subagents", "files", "bytes", "seconds", "days"})
        self.assertTrue(all(isinstance(v, (int, float)) for v in r.values()))
        blob = json.dumps(r).lower()
        self.assertNotIn(SECRET.lower(), blob)
        self.assertNotIn(self.home.lower(), blob)

    def test_cli_writes_json_atomically_mode_600(self):
        out = os.path.join(self.home, "out.json")
        self.assertEqual(hc.main(["--out", out, "--dirs", self.cfg()]), 0)
        self.assertEqual(json.load(open(out)), {"none": True})
        self.write(self.cfg(), "a.jsonl", [user(time.time() - 60, "hi"), assistant(time.time() - 50, "Hello.")])
        self.assertEqual(hc.main(["--out", out, "--dirs", self.cfg()]), 0)
        data = json.load(open(out))
        self.assertEqual(data["files"], 1)
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
        self.assertEqual([n for n in os.listdir(self.home) if n.endswith(".tmp")], [])
        self.assertEqual(hc.main(["--out", os.path.join(self.home, "no", "such", "dir", "o.json"), "--dirs", self.cfg()]), 0)


if __name__ == "__main__":
    unittest.main()
