#!/usr/bin/env python3
"""XLI-430 (owner 11-27): `datastash sync CODE` typed while the session is busy never reached datastash. Claude Code
2.1.332 gives the prompt hook the RUNNING turn's prompt id for a line typed while the session works, and writes the line
either as a "queued" user entry under its own new prompt id (between model steps) or, while a tool call runs, as a
"queued_command" attachment written once that call ends. 1.0.27's checker stopped at the running turn's own entry (other
text) and gave up after 30 s, so no code check ever went out. 1.0.28:
- the checker finds the person's queued line (both forms), with the same typed proof (origin human; the mid-turn form
  only under a terminal Claude Code the hook proved from the process table, never `claude -p`, the desktop app, or a
  Claude Code started by another one), and acts on it within a second of it being written, with no model step;
- the line's result is shown at the next tool call of a busy session (not only at the turn's end);
- agent or tool text, or a line with no typed entry, still sends nothing; one typed line is used once.
No network (test/harness.py). Run: python3 -I test/test_xli430.py"""
import json
import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_email_signin import CODE_REQ, EMAIL, EmailSignIn  # noqa: E402
from test_install import ds  # noqa: E402


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


class BusySession(EmailSignIn):
    def setUp(self):
        super().setUp()
        self.saved_430 = {k: getattr(ds, k, None) for k in ("terminal_host_proven", "EMAIL_CODE_LIFE")}
        ds.terminal_host_proven = lambda chain=None: True
        ds.VERIFY_WAIT = 1

    def tearDown(self):
        for k, v in self.saved_430.items():
            setattr(ds, k, v)
        super().tearDown()

    def hook_only(self, text, pid, sid="s1"):
        """The prompt hook as Claude Code runs it for a line typed while turn `pid` is still running."""
        return self.hook(ds.cmd_prompt, {"session_id": sid, "prompt": text, "hook_event_name": "UserPromptSubmit",
                                         "cwd": self.folder, "prompt_id": pid, "transcript_path": self.transcript(sid)})

    def write(self, v, sid="s1"):
        with open(self.transcript(sid), "a") as f:
            f.write(json.dumps(v) + "\n")

    def midturn(self, text, sid="s1", **change):
        """The real 2.1.332 entry of a line typed while a tool call ran (test/fixtures shape, captured 11-28)."""
        a = {"type": "queued_command", "prompt": text, "source_uuid": "91ec5688-747f-4586-a91d-808c50a00940",
             "delivery_id": "1e8ead5a-9608-48aa-a7d9-30e42bff2194", "commandMode": "prompt", "origin": {"kind": "human"},
             "timestamp": now_iso(), "humanTurn": True}
        v = {"parentUuid": "b92506c5-844f-4ee9-8b12-a63d213fe6b3", "isSidechain": False, "attachment": a,
             "type": "attachment", "uuid": "6dd3dc38-f2cf-406c-81c5-caf82049287a", "timestamp": a["timestamp"],
             "userType": "external", "entrypoint": "cli", "sessionId": sid, "version": "2.1.332"}
        for k, val in change.items():
            if k.startswith("a_"):
                a.pop(k[2:], None) if val is None else a.__setitem__(k[2:], val)
            else:
                v.pop(k, None) if val is None else v.__setitem__(k, val)
        return v

    def email_sent(self):
        _v, pid = self.prompt(f"datastash sync {EMAIL}")
        self.check(pid)
        self.assertEqual(self.paths(), [ds.EMAIL_WAIT_PATH, ds.EMAIL_START_PATH])
        return pid  # the turn that is still running when the code is typed

    def tool_hook(self, sid="s1"):
        return self.hook(ds.cmd_agent_tool, {"session_id": sid, "hook_event_name": "PreToolUse", "tool_name": "Read",
                                             "tool_input": {"file_path": "/tmp/x"}, "cwd": self.folder})

    def code_sent(self):
        return ds.EMAIL_CODE_PATH in self.paths()

    def test_queued_line_under_the_running_turns_id_signs_in(self):
        running = self.email_sent()
        v = self.hook_only("datastash sync 482913", running)
        self.assertNotIn("decision", v or {})  # never blocked: Claude Code must write the line for the proof
        self.write({"type": "user", "promptId": "671e50ec-3651-4aee-8dc1-93edc20c091d", "promptSource": "queued",
                    "origin": {"kind": "human"}, "message": {"role": "user", "content": "datastash sync 482913"},
                    "timestamp": now_iso()})
        self.check(running)
        self.assertTrue(self.code_sent())
        self.assertEqual(self.calls[2][3], {"device_code": CODE_REQ, "code": "482913"})
        self.assertTrue(ds.email_login())
        self.assertEqual(self.tool_hook()["systemMessage"], "datastash: Done.")  # at the next tool call
        self.assertNotIn("systemMessage", self.stop() or {})  # shown once

    def test_midturn_line_written_after_the_tool_call_ends_signs_in_without_a_model_step(self):
        running = self.email_sent()
        self.hook_only("datastash sync 482913", running)
        entry = self.midturn("datastash sync 482913")
        # the running tool call ends after the checker's first wait: Claude Code only then writes the line
        threading.Timer(1.6, self.write, (entry,)).start()
        t0 = time.time()
        self.check(running)
        took = time.time() - t0
        self.assertTrue(self.code_sent())
        self.assertLess(took, 1.6 + 1.5, "the code went out more than 1.5 s after Claude Code wrote the line")
        with open(self.transcript("s1")) as f:
            self.assertFalse(any(json.loads(x).get("type") == "assistant" for x in f))  # no model step anywhere
        self.assertEqual(self.tool_hook()["systemMessage"], "datastash: Done.")

    def test_one_typed_line_is_used_once(self):
        running = self.email_sent()
        self.hook_only("datastash sync 482913", running)
        self.write(self.midturn("datastash sync 482913"))
        self.check(running)
        n = len(self.calls)
        ds.write_private(ds._code_req_path("s1"), json.dumps({"device_code": CODE_REQ, "waiting": "dsdc_" + "w" * 40,
                                                              "until": int(time.time()) + 600}))
        self.hook_only("datastash sync 482913", running)  # the same written line, offered again
        self.check(running)
        self.assertEqual(len(self.calls), n)

    def refused(self, entry=None, terminal=True):
        self.calls.clear()
        ds.terminal_host_proven = lambda chain=None: terminal
        ds.EMAIL_CODE_LIFE = 2
        running = self.email_sent()
        self.hook_only("datastash sync 482913", running)
        if entry is not None:
            self.write(entry)
        self.check(running)
        self.assertFalse(self.code_sent())
        self.assertFalse(ds.email_login())

    def test_no_typed_entry_sends_nothing(self):
        self.refused()

    def test_midturn_line_not_marked_human_sends_nothing(self):
        self.refused(self.midturn("datastash sync 482913", a_origin=None))
        self.refused(self.midturn("datastash sync 482913", a_origin={"kind": "system"}))

    def test_midturn_line_from_a_task_notification_sends_nothing(self):
        self.refused(self.midturn("datastash sync 482913", a_commandMode="task-notification"))
        self.refused(self.midturn("datastash sync 482913", a_humanTurn=None))

    def test_midturn_line_of_a_headless_or_desktop_host_sends_nothing(self):
        self.refused(self.midturn("datastash sync 482913", entrypoint="sdk-cli"))
        self.refused(self.midturn("datastash sync 482913", entrypoint="claude-desktop"))
        self.refused(self.midturn("datastash sync 482913"), terminal=False)

    def test_midturn_line_with_other_text_or_old_sends_nothing(self):
        self.refused(self.midturn("datastash sync 482914"))
        old = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 300))
        self.refused(self.midturn("datastash sync 482913", timestamp=old))

    def test_the_email_line_typed_while_busy_sends_the_code_too(self):
        _v, running = self.prompt("Run the tests")
        self.hook_only(f"datastash sync {EMAIL}", running)
        self.write(self.midturn(f"datastash sync {EMAIL}"))
        self.check(running)
        self.assertEqual(self.paths(), [ds.EMAIL_WAIT_PATH, ds.EMAIL_START_PATH])

    def test_the_model_is_told_to_carry_on_not_to_act(self):
        running = self.email_sent()
        ai = self.hook_only("datastash sync 482913", running)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("carry on with it and say nothing", ai)
        self.assertIn("Make no datastash calls and run no command", ai)
        self.assertNotIn("482913", ai)


for _name in [n for n in vars(EmailSignIn) if n.startswith("test_")]:
    setattr(BusySession, _name, None)  # test/test_email_signin.py runs those; here only the busy-session cases


class TerminalHost(unittest.TestCase):
    def chain(self, *procs):
        return [{"name": n, "bundle": "", "flags": list(f)} for n, f in procs]

    def test_terminal_claude_code(self):
        self.assertTrue(ds.terminal_host_proven(self.chain(("sh", ()), ("claude.exe", ("--permission-mode",)), ("bash", ()),
                                                           ("tmux", ()))))
        self.assertTrue(ds.terminal_host_proven(self.chain(("2.1.332", ()), ("zsh", ()), ("login", ()))))

    def test_headless_or_nested_or_unread(self):
        self.assertFalse(ds.terminal_host_proven(self.chain(("claude", ("-p",)), ("bash", ()))))
        self.assertFalse(ds.terminal_host_proven(self.chain(("node", ("--print",)),)))
        self.assertFalse(ds.terminal_host_proven(self.chain(("claude", ()), ("bash", ()), ("claude.exe", ()))))
        self.assertFalse(ds.terminal_host_proven(self.chain(("python3", ()), ("bash", ()))))
        self.assertFalse(ds.terminal_host_proven([]))


if __name__ == "__main__":
    unittest.main()
