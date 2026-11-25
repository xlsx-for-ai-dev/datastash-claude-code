#!/usr/bin/env python3
"""Offline tests for plugin 1.0.18 (owner 11-25, XLI-413): the plugin saves each exchange itself.
- After the reply is shown, the Stop hook (or the next prompt) starts one background save that reads this session's own
  transcript: the person's last typed message and the assistant's last visible text after it, nothing else (no tool
  input or output, no thinking, no file contents, no side chain).
- It goes out as the connector's own chat_save (POST /mcp tools/call) with this chat's chat_key and a turn the plugin
  gives each typed message once: a retry or a second hook saves nothing twice.
- While datastash can't be reached the save waits in the offline outbox and goes out once.
- Only while session saving is on ("datastash sync" here, or the account's setting when nothing was chosen here); never
  after "do not save my sessions" or with the account's setting off.
- The person is told once per session in the plugin's own line; the model's own chat_save is refused (for the model
  only) while the plugin saves; a shared workbook's chat_save still goes out.
- The skill no longer asks the model for a "Saved" line.
No network; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_chatsave_auto.py"""
import json
import os
import sys
import unittest
import urllib.error
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_chatstash import CK, ON, OFF, ChatBase  # noqa: E402
from test_install import KEY, OWN_TOOL, ds  # noqa: E402

SKILL = os.path.join(HERE, "..", "plugins", "datastash", "skills", "datastash-agent", "SKILL.md")
SAVED = {"saved": True, "message": "Saved. Now give your answer to the person in your reply."}


class AutoSave(ChatBase):
    def setUp(self):
        super().setUp()
        self.answers[ds.MCP_PATH] = (200, {"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": json.dumps(SAVED)}]}})
        ds.record_sessions(True)  # "datastash sync" turned session saving on (here and on the account)
        self.setting = dict(ON)

    def entry(self, sid, v):
        with open(self.transcript(sid), "a") as f:
            f.write(json.dumps(v) + "\n")

    def exchange(self, sid, question, answer, tools=False):
        pid = str(uuid.uuid4())
        self.typed(sid, question, pid)
        if tools:
            self.entry(sid, {"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": "SECRET-THOUGHT"},
                {"type": "text", "text": "Let me look."},
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/etc/SECRET-PATH"}}]}})
            self.entry(sid, {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "SECRET-FILE-CONTENTS"}]}})
            self.entry(sid, {"type": "assistant", "isSidechain": True, "message": {"content": [
                {"type": "text", "text": "SECRET-SUBAGENT"}]}})
        self.entry(sid, {"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}})
        return pid

    def saves(self):
        return [b for (m, p, _k, b) in self.calls if m == "POST" and p == ds.MCP_PATH]

    def run_save(self, sid="s1"):
        return ds.chat_auto_save("web-app", sid, os.path.realpath(self.transcript(sid)), wait=0)

    def stop(self, sid="s1"):
        return self.hook(ds.cmd_status_check, {"session_id": sid, "hook_event_name": "Stop", "cwd": self.folder,
                                               "transcript_path": self.transcript(sid)})

    def test_saves_the_typed_message_and_the_visible_answer_only(self):
        self.setting = dict(ON)
        self.exchange("s1", "What is the capital of France?", "The capital of France is Paris.", tools=True)
        self.assertEqual(self.run_save(), "saved")
        [body] = self.saves()
        self.assertEqual(body["method"], "tools/call")
        self.assertEqual(body["params"]["name"], "chat_save")
        self.assertEqual(body["params"]["arguments"], {"chat_key": CK, "turn": 1,
                                                       "user_message": "What is the capital of France?",
                                                       "ai_reply": "The capital of France is Paris."})
        self.assertNotIn("SECRET", json.dumps(body))

    def test_a_retry_or_second_hook_never_saves_twice_and_turns_go_up(self):
        self.exchange("s1", "q1", "a1")
        self.assertEqual(self.run_save(), "saved")
        self.assertEqual(self.run_save(), "saved already")
        self.exchange("s1", "q2", "a2")
        self.assertEqual(self.run_save(), "saved")
        self.assertEqual([b["params"]["arguments"]["turn"] for b in self.saves()], [1, 2])
        self.assertEqual({b["params"]["arguments"]["chat_key"] for b in self.saves()}, {CK})

    def test_a_typed_message_with_no_answer_yet_is_not_saved(self):
        self.typed("s1", "q1", str(uuid.uuid4()))
        self.assertEqual(self.run_save(), "nothing new")
        self.assertEqual(self.saves(), [])

    def test_a_slash_command_entry_is_not_an_exchange(self):
        self.exchange("s1", "q1", "a1")
        self.assertEqual(self.run_save(), "saved")
        self.typed("s1", "<command-name>/clear</command-name>", str(uuid.uuid4()))
        self.assertEqual(self.run_save(), "saved already")
        self.assertEqual(len(self.saves()), 1)

    def test_offline_the_save_waits_in_the_outbox_and_goes_out_once(self):
        inner = ds.http
        ds.http = lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down"))
        self.exchange("s1", "q1", "a1")
        self.assertEqual(self.run_save(), "kept")
        self.assertEqual(self.run_save(), "saved already")
        [name] = ds.outbox_entries("web-app")
        rec = ds.read_json(os.path.join(ds.outbox_dir("web-app"), name), {})
        self.assertEqual((rec["path"], rec["accept"]), (ds.MCP_PATH, ds.MCP_ACCEPT))
        self.assertEqual(rec["body"]["params"]["arguments"]["turn"], 1)
        ds.http = inner
        got = []
        base = ds.http
        ds.http = lambda m, p, key=None, body=None, form=None, timeout=40, accept=None: (
            got.append((p, accept, body)), base(m, p, key, body, form, timeout, accept))[1]
        self.assertEqual(ds.outbox_send("web-app")["sent"], 1)
        self.assertEqual(ds.outbox_send("web-app")["sent"], 0)
        self.assertEqual([(p, a) for p, a, _b in got], [(ds.MCP_PATH, ds.MCP_ACCEPT)])
        self.assertEqual(got[0][2]["params"]["arguments"]["user_message"], "q1")

    def test_off_after_do_not_save_my_sessions(self):
        ds.record_sessions(False)
        self.setting = dict(ON)
        self.exchange("s1", "q1", "a1")
        self.assertEqual(self.run_save(), "off")
        self.assertIsNone(self.stop())
        self.assertEqual((self.saves(), self.spawned), ([], []))

    def test_the_accounts_setting_off_saves_nothing(self):
        self.setting = dict(OFF)
        self.exchange("s1", "q1", "a1")
        self.assertEqual(self.run_save(), "account off")
        self.assertEqual(self.saves(), [])

    def test_no_choice_here_follows_the_account_setting_this_session_was_told(self):
        c = ds.read_json(ds.CONSENT_PATH, {})
        c.pop("sessions", None)
        ds.write_private(ds.CONSENT_PATH, json.dumps(c))
        self.exchange("s1", "q1", "a1")
        self.assertEqual(self.run_save(), "off")
        self.setting = dict(ON)
        self.start("s1")
        self.assertEqual(self.run_save(), "saved")

    def test_stop_starts_the_background_save_and_tells_the_person_once(self):
        self.exchange("s1", "q1", "a1")
        out = self.stop()
        self.assertEqual(out["systemMessage"], ds.CHAT_AUTO_LINE)
        [(argv, _cwd)] = self.spawned
        self.assertEqual(argv[2:5], ["chat-save", "web-app", "s1"])
        self.assertIsNone(self.stop())  # said once per session
        self.assertEqual(len(self.spawned), 2)
        self.assertEqual(self.stop("s2")["systemMessage"], ds.CHAT_AUTO_LINE)  # a new session is told again

    def test_the_next_prompt_also_starts_it_without_a_line(self):
        self.exchange("s1", "q1", "a1")
        v, _pid = self.prompt("next question")
        self.assertNotIn(ds.CHAT_AUTO_LINE, json.dumps(v or {}))
        self.assertTrue(any(a[2] == "chat-save" for a, _c in self.spawned))

    def test_the_models_own_chat_save_is_refused_for_the_model_only(self):
        self.setting = dict(ON)
        self.start("s1")
        out = self.tool("PreToolUse", "chat_save", {"user_message": "q", "ai_reply": "a", "turn": 1})
        hs = out["hookSpecificOutput"]
        self.assertEqual((hs["permissionDecision"], hs["permissionDecisionReason"]), ("deny", ds.CHAT_SAVE_BY_PLUGIN))
        self.assertNotIn("systemMessage", out)
        shared = self.tool("PreToolUse", "chat_save", {"user_message": "q", "ai_reply": "a", "workbook_id": "w1"})
        self.assertNotEqual((shared or {}).get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

    def test_the_skill_never_asks_for_a_saved_line(self):
        with open(SKILL) as f:
            s = f.read()
        self.assertIn('never write a "Saved" line', s)
        self.assertIn("Never call `chat_save` for this chat", s)
        self.assertNotIn("Each time you save, tell them", s)
        self.assertNotIn("After each reply, save the exchange", ds.CHATSTASH_ON)


if __name__ == "__main__":
    unittest.main()
