#!/usr/bin/env python3
"""Offline tests for XLI-427 (plugin 1.0.30), before testers:
1. "Your setup changed" is told once when the agent's project has its own subagents and `datastash estimate` runs from a
   subfolder (the prompt hook and the command read subagents from the same project root).
2. "done around" on a computer set to UTC shows UTC, labelled (DATASTASH_TZ still wins); doctor and the estimate help
   name DATASTASH_TZ in one plain line.
3. No person-facing line of a fresh install or the email sign-in says /mcp or Authenticate; the sign-in lines come
   from the hooks themselves (systemMessage), not only from the model.
No network, no key; under test/harness.py.
Run: python3 -I test/test_xli427.py"""
import datetime
import io
import json
import os
import re
import sys
import time
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness  # noqa: E402,F401
import test_email_signin  # noqa: E402
import test_xli428  # noqa: E402
from test_status import ds  # noqa: E402

NO_MCP = re.compile(r"/mcp|Authenticate", re.I)


def only_own_tests(cls, base):
    """Run only the tests this class defines, not the ones it inherits from the other file's class."""
    for name in dir(base):
        if name.startswith("test") and name not in cls.__dict__:
            setattr(cls, name, None)
    return cls


class SetupChangedOnce(test_xli428.Recalc):
    """The owner's case from the 1.0.29 review: a project with its own subagents, the estimate run from a subfolder."""

    def setUp(self):
        super().setUp()
        self.agents = os.path.join(self.folder, ".claude", "agents")
        os.makedirs(self.agents, exist_ok=True)
        with open(os.path.join(self.agents, "reviewer.md"), "w") as f:
            f.write("x")
        self.sub = os.path.join(self.folder, "src", "app")
        os.makedirs(self.sub, exist_ok=True)
        self.proj_env = os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.cwd = os.getcwd()
        saved = (ds.est_lookback, ds.est_spans)
        ds.est_lookback = lambda save=True: {}
        ds.est_spans = lambda files: {}

        def restore():
            ds.est_lookback, ds.est_spans = saved
            os.chdir(self.cwd)
            if self.proj_env is not None:
                os.environ["CLAUDE_PROJECT_DIR"] = self.proj_env
        self.addCleanup(restore)

    def estimate_from_subfolder(self):
        os.chdir(self.sub)  # the agent's own Bash call, from a subfolder; Claude Code sets no CLAUDE_PROJECT_DIR there
        try:
            with redirect_stdout(io.StringIO()) as out:
                ds.cmd_estimate(["XLI-12", "--local"])
        finally:
            os.chdir(self.cwd)
        return out.getvalue()

    def ask(self):
        return self.prompt("how long will XLI-12 take?", say=False) or ""

    def test_told_once_from_a_subfolder(self):
        self.assertNotIn(ds.EST_SETUP_CHANGED, self.ask())  # first: the setup is kept, nothing told
        for _ in range(3):  # 1.0.29: the command saw no subagents, the hook saw one: told on every turn
            self.assertNotIn(ds.EST_SETUP_CHANGED, self.estimate_from_subfolder())
            self.assertNotIn(ds.EST_SETUP_CHANGED, self.ask())
        with open(os.path.join(self.agents, "tester.md"), "w") as f:
            f.write("x")  # a real change: a new subagent in the project
        told = [ds.EST_SETUP_CHANGED in self.estimate_from_subfolder()]
        for _ in range(2):
            told += [ds.EST_SETUP_CHANGED in self.ask(), ds.EST_SETUP_CHANGED in self.estimate_from_subfolder()]
        self.assertEqual(told.count(True), 1, told)

    def test_the_root_is_the_agents_linked_folder(self):
        agent = ds._own_agent({"cwd": self.sub})
        self.assertEqual(ds.est_setup_root(self.sub, agent), os.path.realpath(self.folder))
        self.assertEqual(ds.est_setup_root(self.sub), os.path.realpath(self.folder))  # no agent: the nearest .claude/agents
        self.assertEqual(ds.est_setup_root(self.folder, agent), os.path.realpath(self.folder))


only_own_tests(SetupChangedOnce, test_xli428.Recalc)


class UtcLabelled(test_xli428.LocalTime):
    def test_utc_shows_utc_and_the_override_wins(self):
        self.zone("UTC")
        self.assertEqual(ds.est_when(self.T), "Sat 1:16 am UTC")
        self.zone("UTC", ds_tz="America/New_York")
        self.assertEqual(ds.est_when(self.T), "Fri 8:16 pm EST")
        self.zone("America/Chicago")
        self.assertEqual(ds.est_when(self.T), "Fri 7:16 pm CST")

    def test_doctor_and_help_name_the_override(self):
        self.assertIn("DATASTASH_TZ", ds.EST_TZ_LINE)
        self.assertIn(ds.EST_TZ_LINE, ds.EST_USAGE)
        with open(os.path.join(os.path.dirname(HERE), "plugins", "datastash", "scripts", "datastash")) as f:
            self.assertIn('print("note: time zone: " + EST_TZ_LINE)', f.read())  # doctor prints it


only_own_tests(UtcLabelled, test_xli428.LocalTime)


class NoMcpForThePerson(test_email_signin.EmailSignIn):
    """Owner 11-28: no /mcp step, ever. The person's lines in a fresh install and the email sign-in."""

    def shown(self, v):
        """What the person sees from one hook answer: its systemMessage, or a block's reason."""
        v = v or {}
        return " ".join(x for x in (v.get("systemMessage"), v.get("reason") if v.get("decision") == "block" else None)
                        if isinstance(x, str))

    def test_no_person_line_says_mcp_or_authenticate(self):
        seen = []
        v, _pid = self.slash("/datastash:install")  # a fresh computer: the one sign-in line
        seen.append(self.shown(v))
        v, pid = self.prompt(f"datastash sync {test_email_signin.EMAIL}")
        seen.append(self.shown(v))
        self.check(pid)
        seen.append(self.shown(self.stop()))
        v, pid = self.prompt("datastash sync 482913")
        seen.append(self.shown(v))
        self.check(pid)
        seen.append(self.shown(self.stop()))
        v, _pid = self.prompt("hello")
        seen.append(self.shown(v))
        self.assertIn("Done.", " ".join(seen))
        for line in seen:
            self.assertIsNone(NO_MCP.search(line), line)
        for line in (ds.SIGN_IN_FIRST_LINE, ds.CHECK_EMAIL_LINE, ds.CHECKING_CODE_LINE, ds.DONE_LINE, ds.NO_CODE_ASKED_LINE,
                     ds.BAD_CODE_LINE, ds.SIGN_IN_FAILED_LINE, ds.NO_CONNECTOR_LINE, ds.CONNECT_LINE, ds.RESTART_LINE,
                     ds.INSTALL_FAILED_LINE, ds.NAME_TAKEN_LINE, ds.IMPORTED_HINT):
            self.assertIsNone(NO_MCP.search(line), line)

    def test_the_sign_in_lines_come_from_the_hook_itself(self):
        v, pid = self.prompt(f"datastash sync {test_email_signin.EMAIL}")
        self.assertEqual(v["systemMessage"], ds._plain_typed(ds.CHECK_EMAIL_LINE))  # shown with no model turn
        self.check(pid)
        v, pid = self.prompt("datastash sync 482913")
        self.assertEqual(v["systemMessage"], ds.CHECKING_CODE_LINE)
        self.assertIn("Never ask the person for /mcp", v["hookSpecificOutput"]["additionalContext"])
        self.check(pid)
        self.assertEqual(self.stop()["systemMessage"], "datastash: Done. " + ds.NEXT_SESSION_LINE)  # XLI-434

    def test_the_turns_end_never_waits_on_a_line_typed_while_it_runs(self):
        """The real e2e run (Claude Code 2.1.332): a CODE typed during the EMAIL turn's Stop hook is written only once
        that hook ends; its check must not hold that hook for the whole wait."""
        ds.ensure_dirs()
        ds.write_private(ds._pending_path("p-later"), json.dumps({"kind": "sync_code", "sid": "s9", "at": time.time() + 1}))
        ds.write_private(ds._step_busy_path("s9"), json.dumps({"at": time.time() + 1}))
        t = time.time()
        ds.login_step_wait("s9", limit=3)
        self.assertLess(time.time() - t, 1)
        ds.write_private(ds._pending_path("p-earlier"), json.dumps({"kind": "sync_code", "sid": "s9", "at": time.time() - 1}))
        t = time.time()
        ds.login_step_wait("s9", limit=0.6)  # one handed over before the turn's end: waited for
        self.assertGreaterEqual(time.time() - t, 0.5)
        for f in (ds._pending_path("p-later"), ds._pending_path("p-earlier"), ds._step_busy_path("s9")):
            ds._drop(f)

    def test_the_skill_and_the_readme_install_steps_have_no_mcp_step(self):
        root = os.path.dirname(HERE)
        with open(os.path.join(root, "plugins", "datastash", "skills", "datastash-agent", "SKILL.md")) as f:
            quoted = re.findall(r'tell the person exactly: "([^"]+)"', f.read())
        self.assertTrue(quoted)
        for q in quoted:
            self.assertIsNone(NO_MCP.search(q), q)
        with open(os.path.join(root, "README.md")) as f:
            readme = f.read()
        install = readme.split("\n## Install\n", 1)[1].split("\n## ", 1)[0]
        self.assertIsNone(NO_MCP.search(install), install)
        for step in ("/plugin marketplace add xlsx-for-ai-dev/datastash-claude-code", "/plugin install datastash@datastash",
                     "/datastash:install", "datastash sync you@email.com", "datastash sync CODE"):
            self.assertIn(f"**{step}", install)


only_own_tests(NoMcpForThePerson, test_email_signin.EmailSignIn)


if __name__ == "__main__":
    unittest.main()
