#!/usr/bin/env python3
"""Offline tests for XLI-386 (plugin 0.8.0): Clean sessions.
- Two per-person choices, each on or off by itself, kept with this computer's other per-person settings (so they hold
  for all the person's agents here): "commands through subagents" (default on) and "no code in sessions" (default off).
- The session start gives the AI the rules only for what is on.
- A nudge, never a block: a Bash call by the main session (the hook input has no agent_id; a subagent's has one) gets
  one line, "Clean sessions: hand this to a subagent.", at most once per few minutes per session. The command's text is
  never read or repeated.
- The person's typed "no code in sessions", "turn off clean sessions" and "turn on clean sessions" change the choices;
  the agent confirms in one line.
No network, no key; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_clean.py"""
import json
import os
import sys
import time
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import Base, ds  # noqa: E402  (the same harness, plugin copy and fixtures)

SKILL = os.path.join(HERE, "..", "plugins", "datastash", "skills", "datastash-agent", "SKILL.md")
NUDGE = "Clean sessions: hand this to a subagent."
SECRET_CMD = "curl -H 'Authorization: Bearer sk-secret-value' https://example.invalid/x"


def bash(sid="sess1", event="PreToolUse", agent_id=None, cwd=None, cmd=SECRET_CMD):
    v = {"session_id": sid, "hook_event_name": event, "tool_name": "Bash", "tool_input": {"command": cmd},
         "tool_use_id": "toolu_1", "cwd": cwd, "transcript_path": "/nowhere/" + sid + ".jsonl"}
    if agent_id:
        v["agent_id"] = agent_id
        v["agent_type"] = "general-purpose"
    return v


def ctx_of(out):
    return ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""


class Settings(Base):
    def test_defaults_commands_on_code_off(self):
        self.assertEqual(ds.clean_settings(), {"commands": True, "no_code": False})

    def test_each_choice_is_set_by_itself_and_kept_with_the_per_person_settings(self):
        ds.set_clean(no_code=True)
        self.assertEqual(ds.clean_settings(), {"commands": True, "no_code": True})
        ds.set_clean(commands=False)
        self.assertEqual(ds.clean_settings(), {"commands": False, "no_code": True})
        with open(ds.CONSENT_PATH) as f:
            self.assertEqual(json.load(f)["clean"]["commands"], False)

    def test_other_per_person_writes_keep_the_choices(self):
        ds.set_clean(commands=False, no_code=True)
        ds.record_consent([self.folder], [])
        ds.record_left_off([self.folder])
        ds.record_keep_all(False, "typed")
        self.assertEqual(ds.clean_settings(), {"commands": False, "no_code": True})

    def test_a_broken_value_falls_back_to_the_defaults(self):
        ds.write_private(ds.CONSENT_PATH, json.dumps({"clean": {"commands": "yes", "no_code": 1}}))
        self.assertEqual(ds.clean_settings(), {"commands": True, "no_code": False})


class RulesText(Base):
    def test_only_what_is_on_is_shown(self):
        on = ds.clean_session_text()
        self.assertIn("subagent", on)
        self.assertIn("plain words", on)
        self.assertNotIn("code blocks", on)
        ds.set_clean(no_code=True)
        both = ds.clean_session_text()
        self.assertIn("code blocks", both)
        self.assertIn("shell commands", both)
        ds.set_clean(commands=False)
        code_only = ds.clean_session_text()
        self.assertIn("code blocks", code_only)
        self.assertNotIn("shell commands", code_only)
        ds.set_clean(no_code=False)
        self.assertEqual(ds.clean_session_text(), "")

    def test_wording_states_a_choice_never_a_better_way(self):
        ds.set_clean(no_code=True)
        text = ds.clean_session_text().lower()
        for word in ("cleaner", "best practice", "better"):
            self.assertNotIn(word, text)

    def test_session_start_carries_the_rules_for_an_agent_session(self):
        out = self.hook(ds.cmd_check, {"session_id": "sess1", "hook_event_name": "SessionStart", "cwd": self.folder})
        self.assertIn(ds.clean_session_text(), ctx_of(out))
        ds.set_clean(commands=False)
        out = self.hook(ds.cmd_check, {"session_id": "sess1", "hook_event_name": "SessionStart", "cwd": self.folder})
        self.assertNotIn(ds.CLEAN_NOTE, ctx_of(out))


class Nudge(Base):
    def test_main_session_bash_gets_one_line_never_a_block(self):
        out = self.hook(ds.cmd_agent_tool, bash(cwd=self.folder))
        self.assertIn(NUDGE, ctx_of(out))
        self.assertNotIn("decision", out or {})
        self.assertNotIn("permissionDecision", (out or {}).get("hookSpecificOutput", {}))
        self.assertNotIn("sk-secret-value", json.dumps(out))
        self.assertNotIn("example.invalid", json.dumps(out))

    def test_at_most_once_per_few_minutes_per_session(self):
        self.assertIn(NUDGE, ctx_of(self.hook(ds.cmd_agent_tool, bash(cwd=self.folder))))
        self.assertNotIn(NUDGE, ctx_of(self.hook(ds.cmd_agent_tool, bash(cwd=self.folder))))
        self.assertIn(NUDGE, ctx_of(self.hook(ds.cmd_agent_tool, bash(sid="sess2", cwd=self.folder))))
        mark = ds._clean_nudge_path("sess1")
        ds.write_private(mark, json.dumps({"at": int(time.time()) - ds.CLEAN_NUDGE_EVERY - 1}))
        self.assertIn(NUDGE, ctx_of(self.hook(ds.cmd_agent_tool, bash(cwd=self.folder))))

    def test_a_subagent_is_never_nudged(self):
        out = self.hook(ds.cmd_agent_tool, bash(agent_id="a1b2c3", cwd=self.folder))
        self.assertNotIn(NUDGE, ctx_of(out))
        # and a subagent's call does not use up the main session's nudge
        self.assertIn(NUDGE, ctx_of(self.hook(ds.cmd_agent_tool, bash(cwd=self.folder))))

    def test_no_nudge_when_commands_through_subagents_is_off(self):
        ds.set_clean(commands=False, no_code=True)
        self.assertNotIn(NUDGE, ctx_of(self.hook(ds.cmd_agent_tool, bash(cwd=self.folder))))

    def test_only_bash_before_it_runs(self):
        for inp in (bash(event="PostToolUse", cwd=self.folder), bash(event="PostToolUseFailure", cwd=self.folder),
                    dict(bash(cwd=self.folder), tool_name="Read", tool_input={"file_path": "/x"})):
            self.assertNotIn(NUDGE, ctx_of(self.hook(ds.cmd_agent_tool, inp)))

    def test_the_persons_main_session_is_never_nudged(self):
        ds.ensure_dirs()
        ds.write_private(ds._main_path("sess1"), json.dumps({"sid": "sess1"}))
        self.assertNotIn(NUDGE, ctx_of(self.hook(ds.cmd_agent_tool, bash(cwd=self.folder))))


class Phrases(Base):
    def prompt(self, text, sid="sess1"):
        return self.hook(ds.cmd_prompt, {"session_id": sid, "hook_event_name": "UserPromptSubmit", "prompt": text,
                                         "cwd": self.folder, "prompt_id": str(uuid.uuid4()),
                                         "transcript_path": self.transcript(sid)})

    def test_the_phrases_are_recognised_forgivingly(self):
        self.assertEqual(ds.clean_phrase("no code in sessions"), "no_code")
        self.assertEqual(ds.clean_phrase('  "No code in sessions."'), "no_code")
        self.assertEqual(ds.clean_phrase("no code in my session"), "no_code")
        self.assertEqual(ds.clean_phrase("Turn off clean sessions"), "off")
        self.assertEqual(ds.clean_phrase("turn on clean sessions!"), "on")
        self.assertEqual(ds.clean_phrase("code in sessions"), "code")
        for t in ("please explain why there is no code in sessions here", "clean sessions", "", None, "x" * 500):
            self.assertIsNone(ds.clean_phrase(t), t)

    def test_no_code_in_sessions_turns_on_the_second_choice_and_the_agent_confirms(self):
        out = self.prompt("no code in sessions")
        self.assertEqual(ds.clean_settings(), {"commands": True, "no_code": True})
        ctx = ctx_of(out)
        self.assertIn(ds.CLEAN_NOTE, ctx)
        self.assertIn("one line", ctx)
        self.assertIn("code blocks", ctx)  # the rule applies in this session at once

    def test_turn_off_turns_both_off(self):
        ds.set_clean(no_code=True)
        out = self.prompt("turn off clean sessions")
        self.assertEqual(ds.clean_settings(), {"commands": False, "no_code": False})
        self.assertIn("one line", ctx_of(out))
        self.assertIn("off", ctx_of(out))

    def test_turn_on_turns_commands_back_on(self):
        ds.set_clean(commands=False)
        out = self.prompt("turn on clean sessions")
        self.assertEqual(ds.clean_settings(), {"commands": True, "no_code": False})
        self.assertIn("no code in sessions", ctx_of(out))  # how to turn on the second choice

    def test_code_in_sessions_turns_the_second_choice_off(self):
        ds.set_clean(no_code=True)
        self.prompt("code in sessions")
        self.assertEqual(ds.clean_settings(), {"commands": True, "no_code": False})

    def test_an_ordinary_prompt_changes_nothing(self):
        self.prompt("can you turn off clean sessions for the other project later")
        self.assertEqual(ds.clean_settings(), {"commands": True, "no_code": False})


class Skill(unittest.TestCase):
    def test_skill_names_the_phrases_and_follows_the_note(self):
        with open(SKILL) as f:
            body = f.read().split("# Part 2:", 1)[1]
        self.assertTrue("## Clean sessions" in body, "no Clean sessions section in Part 2")
        for phrase in ('"no code in sessions"', '"turn off clean sessions"', '"turn on clean sessions"'):
            self.assertIn(phrase, body)
        self.assertIn("Clean sessions: hand this to a subagent.", body)


if __name__ == "__main__":
    unittest.main()
