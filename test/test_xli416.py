#!/usr/bin/env python3
"""Offline tests for XLI-416 (the function list made true), plugin side:
- A card another agent holds: the session start says plainly to stop and pick another; an Agent call whose description
  names it is refused; while this session's own card (DATASTASH_CARD or the git branch) is held by another agent its
  file edits are refused; its own claim, or a failed lookup, lets the call go.
- The session start loads the Team tab's remembered facts as a short list with source and date; `datastash remember`
  saves one with this agent and today's date.
- The person's own kept skills are written to ~/.claude/skills (never the built-in starter or another person's share),
  only when new or changed, and a skill folder made or changed on this computer is never overwritten.
No network, no key; under test/harness.py.
Run: python3 -I test/test_xli416.py"""
import io
import json
import os
import sys
import time
import unittest
import urllib.parse
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import ds, TMP  # noqa: E402  (the same harness and plugin copy)
import harness  # noqa: E402

WB = "0b6a3f9e-1c2d-4e5f-8a9b-0c1d2e3f4a6c"
KEY = "ldgr_ag_xli416testkeyxli416testkey"
SLUG = "card-agent"


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.folder = os.path.realpath(harness.tmpdir(self, prefix="proj-", dir=TMP))
        ds.ensure_dirs()
        ds.write_private(ds.conf_path(SLUG), json.dumps({"name": SLUG, "workbook_id": WB, "projects": [self.folder]}))
        for n in os.listdir(ds.STATE) if os.path.isdir(ds.STATE) else []:
            if n.startswith("held-"):
                os.unlink(os.path.join(ds.STATE, n))  # test code clearing its own throwaway state
        self.calls = []
        self.jobs = []
        self.rows = []
        self.skills = []
        self.texts = {}
        self.fail = False

        def fake_http(method, path, key=None, body=None, form=None, timeout=40, **kw):
            self.calls.append((method, path, key, json.loads(json.dumps(body))))
            if self.fail:
                raise ds.urllib.error.URLError("down")
            if path.startswith(ds.JOB_PATH + "?"):
                q = urllib.parse.parse_qs(path.split("?", 1)[1])
                return 200, "application/json", json.dumps({"jobs": [j for j in self.jobs if j["job"] == q["job"][0]]})
            if path.startswith(ds.ROWS_PATH + "?"):
                return 200, "application/json", json.dumps({"rows": [{"row_id": i + 1, "cells": c} for i, c in enumerate(self.rows)], "cursor": "1", "more": False})
            if path == ds.ROWS_PATH and method == "POST":
                return 200, "application/json", json.dumps({"row_ids": [1]})
            if path == ds.SKILLS_MCP_PATH:
                name, args = body["params"]["name"], body["params"]["arguments"]
                ans = {"skills": self.skills} if name == "list_skills" else {"name": args["name"], "text": self.texts[args["name"]]}
                return 200, "application/json", json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps(ans)}]}})
            raise AssertionError(f"unexpected call {method} {path}")
        self.saved = {}
        for name, new in (("http", fake_http), ("read_key", lambda s: KEY), ("auto_setup_check", lambda *a, **k: None),
                          ("spawn_detached", lambda argv, cwd: None), ("_git", lambda here, *a: "")):
            self.saved[name] = getattr(ds, name)
            setattr(ds, name, new)
        self.env = {k: os.environ.get(k) for k in ("CLAUDE_PROJECT_DIR", "DATASTASH_CARD")}
        os.environ["CLAUDE_PROJECT_DIR"] = self.folder
        os.environ.pop("DATASTASH_CARD", None)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(ds, k, v)
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def agent(self):
        return ds.agent_for(self.folder)

    def hold(self, card, holder="Beta", mine=False):
        self.jobs.append({"project": "", "job": card, "state": "held", "holder": holder, "fence": 1,
                          **({"mine": True} if mine else {"written_by": holder})})

    def pre(self, tool, ti, **extra):
        return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": ti, "cwd": self.folder,
                "session_id": "s416", **extra}


class HeldCards(Base):
    def test_agent_call_for_a_held_card_is_refused(self):
        self.hold("XLI-12")
        out = ds.held_card_deny(self.pre("Agent", {"description": "Build xli-12 server half", "prompt": "go"}))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        why = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn('card XLI-12 is held by "Beta"', why)
        self.assertIn("Stop: do not work on XLI-12. Pick another card or task", why)

    def test_own_claim_free_card_or_no_card_lets_it_go(self):
        self.hold("XLI-12", holder=SLUG, mine=True)
        self.assertIsNone(ds.held_card_deny(self.pre("Agent", {"description": "Build XLI-12"})))
        self.assertIsNone(ds.held_card_deny(self.pre("Agent", {"description": "Build XLI-13"})))
        self.assertIsNone(ds.held_card_deny(self.pre("Agent", {"description": "look around"})))
        self.assertIsNone(ds.held_card_deny(self.pre("Read", {"file_path": "/x"})))

    def test_edits_refused_while_this_sessions_card_is_held_with_a_short_lived_copy(self):
        self.hold("XLI-77")
        os.environ["DATASTASH_CARD"] = "XLI-77"
        out = ds.held_card_deny(self.pre("Edit", {"file_path": os.path.join(self.folder, "a.py")}))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        n = len(self.calls)
        self.assertIsNotNone(ds.held_card_deny(self.pre("Write", {"file_path": os.path.join(self.folder, "b.py")})))
        self.assertEqual(len(self.calls), n)  # the copy answered: no second lookup within HELD_CHECK_SECONDS
        self.assertIsNone(ds.held_card_deny(self.pre("Edit", {"file_path": "x"}, agent_id="sub-1")))  # a subagent's own card is its description

    def test_branch_names_the_card(self):
        self.hold("XLI-90")
        ds._git = lambda here, *a: "xli-90-fix-thing"
        self.assertIsNotNone(ds.held_card_deny(self.pre("MultiEdit", {"file_path": "x"})))

    def test_a_failed_lookup_lets_the_edit_go(self):
        os.environ["DATASTASH_CARD"] = "XLI-5"
        self.fail = True
        self.assertIsNone(ds.held_card_deny(self.pre("Edit", {"file_path": "x"})))

    def test_session_start_says_stop_and_lists_remembered_facts(self):
        self.hold("XLI-31")
        os.environ["DATASTASH_CARD"] = "XLI-31"
        self.rows = [{"fact": "Deploys go through the deploy door only.", "source": "Alpha", "date": "2026-11-20"},
                     {"fact": "Use role terms, never real names.", "source": "Gamma", "date": "2026-11-26"},
                     {"note": "a row saved by hand"}]
        out = {}
        ds.session_extras(self.agent(), KEY, self.folder, out)
        held, facts = out["lines"]
        self.assertIn('card XLI-31 is held by "Beta"', held)
        self.assertTrue(facts.startswith(ds.FACTS_NOTE))
        self.assertIn("1. note: a row saved by hand", facts)  # newest first
        self.assertIn("2. Use role terms, never real names. (Gamma, 2026-11-26)", facts)
        self.assertIn("3. Deploys go through the deploy door only. (Alpha, 2026-11-20)", facts)

    def test_no_card_and_no_facts_say_nothing(self):
        out = {}
        ds.session_extras(self.agent(), KEY, self.folder, out)
        self.assertEqual(out["lines"], [])


class Remember(Base):
    def run_cmd(self, *args):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            try:
                code = ds.cmd_remember(list(args))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_saves_the_fact_with_source_and_date(self):
        old = os.getcwd()
        os.chdir(self.folder)
        try:
            code, out, _ = self.run_cmd("The", "staging", "box is", "x-live-1.")
        finally:
            os.chdir(old)
        self.assertEqual(code, 0)
        posts = [c for c in self.calls if c[0] == "POST" and c[1] == ds.ROWS_PATH]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2], KEY)
        self.assertEqual(posts[0][3], {"workbook_id": WB, "tab": "Team", "row": {
            "fact": "The staging box is x-live-1.", "source": SLUG, "date": time.strftime("%Y-%m-%d")}})
        self.assertIn("remembered for every agent", out)

    def test_no_fact_is_the_usage(self):
        code, _, err = self.run_cmd()
        self.assertNotEqual(code, 0)
        self.assertEqual([c for c in self.calls if c[0] == "POST"], [])


class Skills(Base):
    def setUp(self):
        super().setUp()
        self.dir = os.path.realpath(harness.tmpdir(self, prefix="skills-", dir=TMP))
        if os.path.exists(ds.SKILLS_SYNC_PATH):
            os.unlink(ds.SKILLS_SYNC_PATH)  # test code clearing its own throwaway state
        self.skills = [
            {"name": "Release Notes", "description": "Write release notes.", "latest_version": 2, "owner": "you", "can_edit": True},
            {"name": "Team Thing", "description": "x", "latest_version": 1, "owner": "someone@example.com", "can_edit": False,
             "written_by": "someone@example.com"},
            {"name": "sync skills", "description": "built in", "latest_version": 1, "owner": ds.SKILL_BUILT_IN_OWNER, "can_edit": False},
        ]
        self.texts = {"Release Notes": "Read the merged PRs and write the notes.", "Team Thing": "not written",
                      "sync skills": "not written"}

    def gets(self):
        return [c[3]["params"]["arguments"]["name"] for c in self.calls if c[1] == ds.SKILLS_MCP_PATH
                and c[3]["params"]["name"] == "get_skill"]

    def test_own_skills_written_once_others_and_built_in_never(self):
        w, skipped = ds.sync_skills(self.agent(), KEY, skills_dir=self.dir)
        self.assertEqual((w, skipped), (1, 0))
        self.assertEqual(sorted(os.listdir(self.dir)), ["release-notes"])
        text = read(os.path.join(self.dir, "release-notes", "SKILL.md"))
        self.assertTrue(text.startswith('---\nname: "release-notes"\ndescription: "Write release notes."\n---\n'))
        self.assertIn("Read the merged PRs and write the notes.", text)
        self.assertEqual(self.gets(), ["Release Notes"])
        self.calls.clear()
        self.assertEqual(ds.sync_skills(self.agent(), KEY, skills_dir=self.dir), (0, 0))
        self.assertEqual(self.gets(), [])  # same version: nothing fetched again
        self.skills[0]["latest_version"] = 3
        self.texts["Release Notes"] = "---\nname: release-notes\ndescription: v3\n---\nNew steps.\n"
        self.assertEqual(ds.sync_skills(self.agent(), KEY, skills_dir=self.dir), (1, 0))
        self.assertEqual(read(os.path.join(self.dir, "release-notes", "SKILL.md")), self.texts["Release Notes"])

    def test_a_skill_made_or_changed_here_is_never_overwritten(self):
        os.makedirs(os.path.join(self.dir, "release-notes"))
        with open(os.path.join(self.dir, "release-notes", "SKILL.md"), "w") as f:
            f.write("my own local skill\n")
        self.assertEqual(ds.sync_skills(self.agent(), KEY, skills_dir=self.dir), (0, 1))
        self.assertEqual(read(os.path.join(self.dir, "release-notes", "SKILL.md")), "my own local skill\n")


if __name__ == "__main__":
    unittest.main()
