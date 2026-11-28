#!/usr/bin/env python3
"""Offline tests for XLI-432 (plugin 1.0.37): typed-proof hardening from the 1.0.28 and 1.0.34 reviews.
- Headless check: any single-dash short-flag group holding p (-p, -dp, -pd, -xp) and any --print... form is headless,
  in the terminal and the desktop process checks.
- A queued line Claude Code 2.1.332 marks as not typed here (fromAnotherSession, textNotTyped, fromConnector,
  hostsOwnTurn, promptRewritten, verifiedSlackHumanTurn, isMeta) is refused, in both queued forms.
- Watcher paths: ~// and any /proc or /dev/fd path are refused; ~/ is expanded the way bash does (HOME + rest).
- The allowed watcher's sha256 is pinned; once the file changes the main session refuses it with a plain line until
  the person types the allow phrase again.
No network, no key; a throwaway HOME (test/harness.py). Run: python3 -I test/test_xli432.py"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_xli430 import BusySession, now_iso  # noqa: E402
from test_xli437 import Watcher, PHRASE  # noqa: E402
from test_clean import denied  # noqa: E402
from test_install import ds  # noqa: E402

MARKS = ("fromAnotherSession", "textNotTyped", "fromConnector", "hostsOwnTurn", "promptRewritten",
         "verifiedSlackHumanTurn", "isMeta")


def chain(*procs):
    return [{"name": n, "bundle": "", "flags": list(f)} for n, f in procs]


class Headless(unittest.TestCase):
    def test_combined_short_flags_with_p_are_headless(self):
        for flags in (("-p",), ("-dp",), ("-pd",), ("-xp",), ("-cpv",), ("--verbose", "-dp"), ("--print",),
                      ("--printx",), ("--print-mode",)):
            with self.subTest(flags=flags):
                self.assertTrue(ds._headless(list(flags)))
                self.assertFalse(ds.terminal_host_proven(chain(("claude", flags), ("bash", ()))))
                self.assertFalse(ds.terminal_host_proven(chain(("2.1.332", flags), ("zsh", ()))))

    def test_interactive_flags_stay_terminal(self):
        for flags in ((), ("-c",), ("-d",), ("-r",), ("--permission-mode",), ("--dangerously-skip-permissions",),
                      ("--model", "-c"), ("--resume",)):
            with self.subTest(flags=flags):
                self.assertFalse(ds._headless(list(flags)))
                self.assertTrue(ds.terminal_host_proven(chain(("claude", flags), ("bash", ()), ("tmux", ()))))

    def test_a_real_claude_dp_process_row_is_headless(self):
        rows = {os.getppid(): (4242, "/usr/local/bin/claude", ["claude", "-dp", "hi"]), 4242: (1, "/bin/bash", ["bash"])}
        saved = ds._proc_row
        try:
            ds._proc_row = lambda pid: rows.get(pid)
            c = ds.process_chain()
            self.assertEqual(c[0]["flags"], ["-dp"])
            self.assertFalse(ds.terminal_host_proven(c))
            rows[os.getppid()] = (4242, "/usr/local/bin/claude", ["claude", "--print=json"])
            self.assertFalse(ds.terminal_host_proven(ds.process_chain()))
            rows[os.getppid()] = (4242, "/usr/local/bin/claude", ["claude", "-c"])
            self.assertTrue(ds.terminal_host_proven(ds.process_chain()))
        finally:
            ds._proc_row = saved

    def test_sixteen_other_options_before_p_are_still_headless(self):
        """1.0.37 review MEDIUM-A: headless is read over the whole argv; only the doctor's list keeps 16 names."""
        others = [f"--opt{n}" for n in range(20)]
        rows = {os.getppid(): (4242, "/usr/local/bin/claude", ["claude"] + others + ["-p", "hi"]),
                4242: (1, "/bin/bash", ["bash"])}
        saved = ds._proc_row
        try:
            ds._proc_row = lambda pid: rows.get(pid)
            c = ds.process_chain()
            self.assertEqual(len(c[0]["flags"]), 16)
            self.assertNotIn("-p", c[0]["flags"])
            self.assertTrue(c[0]["headless"])
            self.assertFalse(ds.terminal_host_proven(c))
            saved_chain = ds.DESKTOP_APP_CHAIN
            try:
                ds.DESKTOP_APP_CHAIN = (("bash", ""),)
                self.assertFalse(ds.desktop_host_proven(c))
            finally:
                ds.DESKTOP_APP_CHAIN = saved_chain
            rows[os.getppid()] = (4242, "/usr/local/bin/claude", ["claude"] + others + ["--print=json"])
            self.assertFalse(ds.terminal_host_proven(ds.process_chain()))
            rows[os.getppid()] = (4242, "/usr/local/bin/claude", ["claude"] + others + ["-c"])
            c = ds.process_chain()
            self.assertFalse(c[0]["headless"])
            self.assertTrue(ds.terminal_host_proven(c))
        finally:
            ds._proc_row = saved

    def test_desktop_path_stays_closed(self):
        self.assertEqual(ds.DESKTOP_APP_CHAIN, ())
        self.assertFalse(ds.desktop_host_proven(chain(("claude", ()), ("Claude", ()))))
        saved = ds.DESKTOP_APP_CHAIN
        try:
            ds.DESKTOP_APP_CHAIN = (("Claude", ""),)
            self.assertTrue(ds.desktop_host_proven(chain(("claude", ()), ("Claude", ()))))
            self.assertFalse(ds.desktop_host_proven(chain(("claude", ("-dp",)), ("Claude", ()))))
            self.assertFalse(ds.desktop_host_proven(chain(("claude", ("--print-x",)), ("Claude", ()))))
        finally:
            ds.DESKTOP_APP_CHAIN = saved


class NotTypedHere(BusySession):
    def queued_user(self, **marks):
        v = {"type": "user", "promptId": "671e50ec-3651-4aee-8dc1-93edc20c091d", "promptSource": "queued",
             "origin": {"kind": "human"}, "message": {"role": "user", "content": "datastash sync 482913"},
             "timestamp": now_iso()}
        v.update(marks)
        return v

    def test_plain_midturn_line_still_signs_in(self):
        self.assertTrue(ds._midturn_typed(self.midturn("datastash sync 482913"), "datastash sync 482913"))
        self.assertTrue(ds._midturn_typed(self.midturn("datastash sync 482913", a_fromConnector=False,
                                                       a_isMeta=False), "datastash sync 482913"))

    def test_marked_midturn_lines_are_refused(self):
        for k in MARKS:
            with self.subTest(mark=k):
                e = self.midturn("datastash sync 482913", **{"a_" + k: True})
                self.assertFalse(ds._midturn_typed(e, "datastash sync 482913"))
                self.refused(e)
                e = self.midturn("datastash sync 482913", **{k: True})  # on the outer entry too
                self.assertFalse(ds._midturn_typed(e, "datastash sync 482913"))

    def test_marked_queued_user_entries_are_refused(self):
        for k in MARKS:
            with self.subTest(mark=k):
                self.refused(self.queued_user(**{k: True}))

    def test_unmarked_queued_user_entry_still_signs_in(self):
        running = self.email_sent()
        self.hook_only("datastash sync 482913", running)
        self.write(self.queued_user(fromAnotherSession=False))
        self.check(running)
        self.assertTrue(self.code_sent())


class MarksOnTheMainPath(unittest.TestCase):
    """1.0.37 review MEDIUM-B: the user entry under the hook's own prompt id is refused when it carries a mark."""
    PID = "5b1d0a3e-2c4f-4e8a-9d6b-7f1e2a3c4d5e"
    TEXT = "datastash sync 482913"

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="xli432-main-")
        self.tp = os.path.join(self.dir, "s1.jsonl")

    def tearDown(self):
        import shutil
        shutil.move(self.dir, self.dir + ".done")  # never rm: the throwaway dir is left beside itself

    def find(self, source="typed", **marks):
        v = {"type": "user", "promptId": self.PID, "promptSource": source, "origin": {"kind": "human"},
             "message": {"role": "user", "content": self.TEXT}, "timestamp": now_iso()}
        v.update(marks)
        with open(self.tp, "w") as f:
            f.write(json.dumps(v) + "\n")
        return [ds.find_typed(self.tp, self.PID, self.TEXT), ds.find_typed(self.tp, self.PID, self.TEXT, at=ds.time.time())]

    def test_plain_typed_line_is_accepted(self):
        for source in ("typed", "queued"):
            for got in self.find(source) + self.find(source, fromConnector=False, isMeta=None):
                self.assertIsInstance(got, tuple, source)
                self.assertEqual(got[0]["promptId"], self.PID)

    def test_each_mark_is_refused(self):
        for k in ("fromAnotherSession", "fromConnector", "textNotTyped", "promptRewritten", "verifiedSlackHumanTurn",
                  "hostsOwnTurn"):
            for source in ("typed", "queued"):
                with self.subTest(mark=k, source=source):
                    self.assertEqual(self.find(source, **{k: True}), ["not person", "not person"])


for _name in [n for n in vars(BusySession) if n.startswith("test_")]:
    setattr(NotTypedHere, _name, None)  # test/test_xli430.py runs those


class WatcherPaths(Watcher):
    def test_tilde_double_slash_is_refused(self):
        self.assertIsNone(ds.watcher_resolve("~//usr/local/bin/w"))
        self.assertIsNone(ds.watcher_resolve("~//bin/true"))
        self.assertIn("Not done", self.prompt("datastash allow watcher ~//bin/true"))
        self.assertEqual(ds.watchers(), [])
        # allowed outside home, then offered as ~//ABS (bash would run HOME//ABS): refused
        self.allow("datastash allow watcher /bin/true")
        self.assertFalse(denied(self.monitor("/bin/true")))
        self.assertTrue(denied(self.monitor("~//bin/true")))
        self.assertTrue(denied(self.bg("~//bin/true")))

    def test_tilde_expands_as_bash_does(self):
        self.assertEqual(ds.watcher_resolve("~/bin/inbox-new")[0], self.prog)
        self.assertEqual(ds.watcher_resolve("~/bin/./inbox-new")[0], self.prog)

    def test_proc_and_dev_fd_paths_are_refused(self):
        real = os.path.realpath(self.prog)
        for p in ("/proc/self/root" + real, "//proc/self/root" + real, "/proc/self/cwd/x", "/dev/fd/3", "/dev/fd/0",
                  "/dev/stdin", "/dev/fd"):
            with self.subTest(path=p):
                self.assertIsNone(ds.watcher_resolve(p))
                self.assertIn("Not done", self.prompt("datastash allow watcher " + p))
        self.assertEqual(ds.watchers(), [])
        self.allow()
        for p in ("/proc/self/root" + real, "/proc/self/cwd/bin/inbox-new", "/dev/fd/3"):
            self.assertTrue(denied(self.monitor(p)), p)
            self.assertTrue(denied(self.bg(p)), p)

    def test_a_link_through_proc_is_refused(self):
        link = os.path.join(self.bin, "via-proc")
        os.symlink("/proc/self/cwd/bin/inbox-new", link)
        self.assertIsNone(ds.watcher_resolve("~/bin/via-proc"))
        self.assertIn("Not done", self.prompt("datastash allow watcher ~/bin/via-proc"))
        self.assertEqual(ds.watchers(), [])
        ok = os.path.join(self.bin, "plain-link")
        os.symlink(self.prog, ok)
        self.assertEqual(ds.watcher_resolve("~/bin/plain-link")[1], os.path.realpath(self.prog))

    def test_a_stored_proc_row_allows_nothing(self):
        ds._write_watchers([{"path": "/proc/self/root" + self.prog, "real": self.prog, "args": [],
                             "sha256": ds.watcher_sha(self.prog)}])
        self.assertTrue(denied(self.monitor("/proc/self/root" + self.prog)))


class WatcherPin(Watcher):
    def changed_line(self, out):
        hs = (out or {}).get("hookSpecificOutput") or {}
        return hs.get("permissionDecision") == "deny" and "changed since you allowed it" in hs.get(
            "permissionDecisionReason", "") and "changed since you allowed it" in (out or {}).get("systemMessage", "")

    def test_allow_pins_the_files_sha256(self):
        self.allow()
        rows = ds.watchers()
        self.assertEqual(len(rows), 1)
        self.assertRegex(rows[0]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(rows[0]["sha256"], ds.watcher_sha(self.prog))

    def test_a_changed_file_is_refused_with_a_plain_line_until_re_allowed(self):
        self.allow()
        self.assertFalse(denied(self.monitor("~/bin/inbox-new")))
        with open(self.prog, "a") as f:
            f.write("echo changed\n")
        for out in (self.monitor("~/bin/inbox-new"), self.bg("~/bin/inbox-new"), self.bg(self.prog)):
            self.assertTrue(self.changed_line(out), out)
            self.assertIn("datastash allow watcher ~/bin/inbox-new", out["systemMessage"].replace(self.prog,
                                                                                                "~/bin/inbox-new"))
        self.assertFalse(ds.clean_watcher_ok("Monitor", {"command": "~/bin/inbox-new"}))
        self.allow()  # the person types it again
        self.assertFalse(denied(self.monitor("~/bin/inbox-new")))
        self.assertFalse(self.changed_line(self.monitor("~/bin/inbox-new")))
        self.assertEqual(len(ds.watchers()), 1)

    def test_a_replaced_file_at_the_same_path_is_refused(self):
        self.allow()
        tmp = self.prog + ".new"
        with open(tmp, "w") as f:
            f.write("#!/bin/sh\necho other\n")
        os.chmod(tmp, 0o755)
        os.replace(tmp, self.prog)
        self.assertTrue(self.changed_line(self.monitor("~/bin/inbox-new")))

    def test_a_row_without_a_pin_counts_as_changed(self):
        ds._write_watchers([{"path": self.prog, "real": self.prog, "args": [], "at": 1}])  # a 1.0.34-1.0.36 row
        self.assertTrue(self.changed_line(self.monitor("~/bin/inbox-new")))
        self.allow()
        self.assertFalse(denied(self.monitor("~/bin/inbox-new")))

    def test_the_changed_line_only_for_the_allowed_command(self):
        self.allow()
        with open(self.prog, "a") as f:
            f.write("echo changed\n")
        for cmd in ("~/bin/inbox-new --x", "~/bin/other-watcher", "touch F"):
            out = self.monitor(cmd)
            self.assertTrue(denied(out), cmd)
            self.assertNotIn("systemMessage", out or {})
        self.assertTrue(denied(self.call("Bash", {"command": "~/bin/inbox-new"})))  # foreground: the one line

    def test_unchanged_file_runs_and_clean_off_changes_nothing(self):
        self.allow()
        self.assertFalse(denied(self.bg("~/bin/inbox-new")))
        with open(self.prog, "a") as f:
            f.write("echo changed\n")
        ds.set_clean(commands=False, no_code=False)
        self.assertIsNone(ds.clean_block({"hook_event_name": "PreToolUse", "session_id": "sess1", "tool_name": "Monitor",
                                          "tool_input": {"command": "~/bin/inbox-new"}}))


for _cls in (WatcherPaths, WatcherPin):
    for _name in [n for n in vars(Watcher) if n.startswith("test_")]:
        setattr(_cls, _name, None)  # test/test_xli437.py runs those


if __name__ == "__main__":
    unittest.main()
