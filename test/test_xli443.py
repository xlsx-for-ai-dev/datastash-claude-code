#!/usr/bin/env python3
"""Offline tests for XLI-443 (plugin 1.0.42): quiet nudges. The owner never wants to see datastash's notes to the
agent ("i just don't need to see it"); Claude Code 2.1.332 shows the person every Stop-hook block and every asyncRewake
wake, and shows nothing of a UserPromptSubmit's or PostToolUse's additionalContext.
- The status reminder, the install note and the inbox note are kept for their own session and given to the agent only
  as additionalContext at its next prompt or next tool call (once; never to a subagent's call). The Stop hook never
  blocks; the hook listener never exits 2 for an agent's inbox note.
- A prompt the plugin answers itself (a block) keeps the notes for the next one.
- With clean sessions on, the main session runs its own status write (printf '%s' '<json>' | datastash status-set -),
  and the reminder says so; nothing else is let through that was not before.
- The person's own lines from the Stop hook ("Done." and the sign-in results, 1.0.30) stay as they are.
No network, no key; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_xli443.py"""
import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import Base, ds  # noqa: E402  (the same harness, plugin copy and fixtures)
from test_clean import bash, denied  # noqa: E402

STATUS_CMD = "printf '%s' '{\"doing\": \"Fixing the login page.\", \"tests\": \"12 pass\"}' | datastash status-set -"


class Quiet(Base):
    def stop(self, **kw):
        return self.hook(ds.cmd_status_check, dict({"session_id": "sess1", "cwd": self.folder}, **kw))

    def post(self, sid="sess1", **kw):
        inp = {"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Read", "cwd": self.folder,
               "tool_input": {"file_path": os.path.join(self.folder, "a.txt")}, "tool_response": "x"}
        return self.hook(ds.cmd_agent_tool, dict(inp, **kw))

    def due(self):
        """A turn that edited a file and has no status yet: the reminder is due at its end."""
        ds.turn_started("sess1")
        time.sleep(0.01)
        ds.hook_input = lambda: {"session_id": "sess1", "hook_event_name": "PostToolUse", "tool_name": "Edit",
                                 "tool_input": {"file_path": os.path.join(self.folder, "a.txt")}}
        self.run_cmd(ds.cmd_agent_tool, [])

    def ctx(self, out):
        return ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""

    def test_the_stop_hook_never_blocks_and_shows_nothing_of_the_reminder(self):
        self.due()
        out = self.stop()
        self.assertIsNone(out)  # nothing at all for Claude Code to show the person
        self.assertTrue(os.path.exists(ds._nudge_path("sess1", "status")))

    def test_the_reminder_reaches_the_agent_at_its_next_tool_call_once(self):
        self.due()
        self.stop()
        got = self.ctx(self.post())
        self.assertIn("datastash status-set -", got)
        self.assertIn("the person never sees this note", got)
        self.assertEqual(self.ctx(self.post()), "")  # given once
        self.assertFalse(os.path.exists(ds._nudge_path("sess1", "status")))

    def test_the_reminder_reaches_the_agent_at_its_next_prompt(self):
        self.due()
        self.stop()
        out = self.hook(ds.cmd_prompt, {"session_id": "sess1", "prompt": "carry on", "hook_event_name":
                                        "UserPromptSubmit", "cwd": self.folder, "prompt_id": "p1",
                                        "transcript_path": self.transcript("sess1")})
        self.assertNotEqual(out.get("decision"), "block")
        self.assertNotIn("systemMessage", out)  # a model-only note: never the person's line
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("datastash status-set -", out["hookSpecificOutput"]["additionalContext"])

    def test_a_subagents_tool_call_never_takes_it(self):
        self.due()
        self.stop()
        self.assertEqual(self.ctx(self.post(agent_id="a1b2c3", agent_type="general-purpose")), "")
        self.assertIn("datastash status-set -", self.ctx(self.post()))

    def test_another_sessions_call_never_takes_it(self):
        self.due()
        self.stop()
        self.assertEqual(self.ctx(self.post(sid="sess2")), "")
        self.assertIn("datastash status-set -", self.ctx(self.post()))

    def test_a_prompt_the_plugin_answers_itself_keeps_it(self):
        ds.nudge_save("sess1", "status", "QUIET-NOTE")
        saved = ds.command_prompt
        ds.command_prompt = lambda inp: ("The plugin's own line.", None, True)
        try:
            out = self.hook(ds.cmd_prompt, {"session_id": "sess1", "prompt": "/datastash:install",
                                            "hook_event_name": "UserPromptSubmit", "cwd": self.folder})
        finally:
            ds.command_prompt = saved
        self.assertEqual(out["decision"], "block")
        self.assertNotIn("QUIET-NOTE", json.dumps(out))
        self.assertIn("QUIET-NOTE", self.ctx(self.post()))

    def test_other_context_on_the_same_call_is_kept(self):
        ds.nudge_save("sess1", "inbox", "INBOX-NOTE")
        ds.nudge_save("sess1", "status", "STATUS-NOTE")
        saved = ds._tool_hook

        def tool(inp):
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "OTHER"}}))
            return 0
        ds._tool_hook = tool
        try:
            got = self.ctx(self.post())
        finally:
            ds._tool_hook = saved
        self.assertEqual(got, "OTHER STATUS-NOTE INBOX-NOTE")

    def test_a_stale_note_is_dropped(self):
        ds.nudge_save("sess1", "inbox", "OLD-NOTE")
        old = time.time() - ds.NUDGE_MAX_AGE - 60
        os.utime(ds._nudge_path("sess1", "inbox"), (old, old))
        self.assertEqual(ds.nudges_take("sess1"), "")
        self.assertFalse(os.path.exists(ds._nudge_path("sess1", "inbox")))

    def test_a_pre_tool_call_never_takes_it(self):
        ds.nudge_save("sess1", "status", "QUIET-NOTE")
        out = self.hook(ds.cmd_agent_tool, {"session_id": "sess1", "hook_event_name": "PreToolUse", "tool_name": "Read",
                                            "tool_input": {"file_path": "a.txt"}, "cwd": self.folder})
        self.assertNotIn("QUIET-NOTE", json.dumps(out or {}))
        self.assertIn("QUIET-NOTE", self.ctx(self.post()))

    def test_the_persons_own_stop_lines_stay(self):
        """1.0.30: a typed sign-in's result ("Done.") still shows the person from the turn's end."""
        ds.add_outcome("sess1", "Done.")
        out = self.stop()
        self.assertEqual(out, {"systemMessage": "datastash: Done."})

    def test_no_stop_hook_text_in_the_plugin_blocks(self):
        """No code path left in the Stop hook builds a decision (a block the person sees)."""
        with open(os.path.join(HERE, "..", "plugins", "datastash", "scripts", "datastash")) as f:
            src = f.read()
        body = src[src.index("def _status_check():"):src.index("def _status_remind(")]
        self.assertNotIn('"decision"', body)
        remind = src[src.index("def _status_remind("):src.index("# --- the person's own typed words")]
        self.assertNotIn('"decision"', remind)


class CleanStatusSet(Base):
    def setUp(self):
        super().setUp()
        ds.set_clean(commands=True, no_code=False)

    def call(self, cmd, **kw):
        return self.hook(ds.cmd_agent_tool, bash(cmd=cmd, cwd=self.folder, **kw))

    def test_status_set_runs_in_the_main_session(self):
        self.assertFalse(denied(self.call(STATUS_CMD)))
        self.assertFalse(denied(self.call("datastash status-set --same")))

    def test_the_reminder_gives_that_form_and_no_subagent(self):
        self.due_and_stop()
        got = ds.nudges_take("sess1")
        self.assertTrue(got.endswith(ds.STATUS_CLEAN_FORM))
        self.assertIn("| datastash status-set -", got)
        self.assertNotIn("Agent tool", got)

    def due_and_stop(self):
        ds.turn_started("sess1")
        time.sleep(0.01)
        ds.hook_input = lambda: {"session_id": "sess1", "hook_event_name": "PostToolUse", "tool_name": "Edit",
                                 "tool_input": {"file_path": os.path.join(self.folder, "a.txt")}}
        self.run_cmd(ds.cmd_agent_tool, [])
        self.hook(ds.cmd_status_check, {"session_id": "sess1", "cwd": self.folder})

    def test_nothing_else_is_widened(self):
        for cmd in (STATUS_CMD + "; touch F", STATUS_CMD + " && touch F", "datastash status-set - < notes.json",
                    "cat notes.json | datastash status-set -", "echo '{}' | datastash status-set -",
                    "datastash status-set - --project /tmp/x; touch F", "datastash status-send web-app 1",
                    "printf '%s' '{}' | datastash api POST /api/v1/rows/upsert -", "touch F"):
            self.assertTrue(denied(self.call(cmd)), cmd)

    def test_clean_sessions_off_reminder_has_no_clean_form(self):
        ds.set_clean(commands=False, no_code=False)
        self.due_and_stop()
        got = ds.nudges_take("sess1")
        self.assertIn("datastash status-set -", got)
        self.assertNotIn(ds.STATUS_CLEAN_FORM, got)


class HooksFile(unittest.TestCase):
    def test_stop_hooks_are_unchanged_in_shape(self):
        """The Stop hook still runs status-check (the person's own lines, the chat save) and the listener."""
        with open(os.path.join(HERE, "..", "plugins", "datastash", "hooks", "hooks.json")) as f:
            hooks = json.load(f)["hooks"]
        stop = [h["args"][1] for h in hooks["Stop"][0]["hooks"]]
        self.assertEqual(stop, ["status-check", "listen"])


if __name__ == "__main__":
    unittest.main()
