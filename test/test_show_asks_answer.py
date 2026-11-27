#!/usr/bin/env python3
"""Offline tests for XLI-415's leftovers: answer from the Show asks list, and the welcome-back asks.
- After "show asks", the person's typed "N: <answer>" ("answer N: yes", "do N") on an ask with an instance goes through
  the "Answer my agents" path: proved typed in the transcript, signed, posted to /api/v1/person/status/answer with that
  ask's own workbook and instance; an ask with options takes one of them; a wait (no instance) sends nothing.
- At session start and on the first prompt after a long gap, the person sees their open asks, blocking first, in a
  short list (nothing when there are none); those numbers answer the same way.
No network, no key; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_show_asks_answer.py"""
import hashlib
import hmac
import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness  # noqa: E402,F401
from test_status import Base, ds, KEY, GRANT, WB  # noqa: E402

WB2 = "bbbbbbbb-0000-4000-8000-00000000000b"
I_P = "cccccccc-0000-4000-8000-000000000001"
I_O = "cccccccc-0000-4000-8000-000000000002"
LABEL = "datastash-typed-answer/v1"
ASKS = {"open": 4, "note": "n", "items": [
    {"n": 1, "agent": "Alpha", "of": "Beta", "ask": "Pick up this hand-off: Ship it", "blocking": False, "kind": "handoff"},
    {"n": 2, "agent": "Beta", "of": "Alpha", "ask": "the schema", "blocking": True, "kind": "wait"},
    {"n": 3, "agent": "Gamma", "of": "you", "ask": "Ship today?", "blocking": True, "kind": "person",
     "workbook_id": WB2, "instance": I_P},
    {"n": 4, "agent": "Gamma", "of": "you", "ask": "Which region?", "blocking": False, "kind": "person",
     "workbook_id": WB2, "instance": I_O, "options": ["EU", "US"]}]}


def mac(answer, inst, p):
    js = json.dumps(answer, ensure_ascii=False, separators=(",", ":"))
    lines = [LABEL, GRANT.lower(), WB2, inst, hashlib.sha256(js.encode()).hexdigest(), p["prompt_id"], p["typed_sha256"],
             str(p["typed_at"]), str(p["iat"])]
    return hmac.new(KEY.encode(), "\n".join(lines).encode(), "sha256").hexdigest()


class AsksBase(Base):
    def setUp(self):
        super().setUp()
        self.asks = json.loads(json.dumps(ASKS))
        self.reads = 0
        self.saved_a = (ds._asks_read, ds.read_answer_key, ds.answer_key_kept)
        ds._asks_read = self.read
        ds.read_answer_key = lambda: (KEY, GRANT)
        ds.answer_key_kept = lambda: True
        self.addCleanup(self.restore_a)
        base = ds.http

        def http(method, path, key=None, body=None, form=None, timeout=40):
            if path == ds.PERSON_ANSWER_PATH:
                self.calls.append((method, path, key, body))
                return 200, "application/json", json.dumps({"ok": True})
            return base(method, path, key, body, form, timeout)
        ds.http = http

    def read(self):
        self.reads += 1
        return self.asks

    def restore_a(self):
        ds._asks_read, ds.read_answer_key, ds.answer_key_kept = self.saved_a

    def sent(self):
        return [c[3] for c in self.calls if c[1] == ds.PERSON_ANSWER_PATH]


class AnswerFromList(AsksBase):
    def test_answer_n_after_show_asks_is_signed_and_sent_to_that_ask(self):
        shown = self.prompt("show asks")
        self.assertIn("To answer one, type **N: your answer**", shown)
        note = self.prompt("answer 3: yes")
        self.assertIn("ask 3, from Gamma", note)
        [body] = self.sent()
        self.assertEqual((body["workbook_id"], body["instance"], body["answer"]), (WB2, I_P, "yes"))
        p = body["typed_proof"]
        self.assertEqual(p["mac"], mac("yes", I_P, p))
        self.assertEqual(self.outcomes(), ['Sent "yes" to Gamma.'])

    def test_options_take_one_of_them(self):
        self.prompt("show asks")
        shown = self.prompt("4: Asia")
        self.assertIn("takes exactly one of: EU, US", shown)
        self.assertEqual(self.sent(), [])
        self.prompt("show asks")
        self.prompt("4: eu")
        self.assertEqual([(b["instance"], b["answer"]) for b in self.sent()], [(I_O, "EU")])

    def test_a_wait_sends_nothing(self):
        self.prompt("show asks")
        shown = self.prompt("2: yes")
        self.assertIn("is a wait with nothing to answer here", shown)
        self.assertEqual(self.sent(), [])

    def test_no_list_no_answer(self):
        self.prompt("3: yes")
        self.assertEqual(self.sent(), [])


class WelcomeBack(AsksBase):
    def setUp(self):
        super().setUp()
        self.env.setdefault("CLAUDE_CODE_ENTRYPOINT", os.environ.get("CLAUDE_CODE_ENTRYPOINT"))
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"  # a person at an interactive Claude Code session

    def gap(self, sid="sess1"):
        old = time.time() - ds.WELCOME_GAP - 5
        os.utime(ds._turn_path(sid), (old, old))

    def assert_no_welcome_after_a_gap(self):
        """1.0.22 review M1: the first prompt after a gap makes no datastash call, shows no list, saves no numbers, and
        a typed "3: yes" sends nothing."""
        self.prompt("hello")
        self.gap()
        n_calls = len(self.calls)
        self.prompt("back again")
        self.assertEqual(self.reads, 0)
        self.assertEqual(self.calls[n_calls:], [])
        self.assertNotIn(ds.WELCOME_HEAD, (self.last_hook or {}).get("systemMessage") or "")
        self.assertNotIn("welcome-back", ((self.last_hook or {}).get("hookSpecificOutput") or {})
                         .get("additionalContext") or "")
        self.assertNotIn("3", (ds.read_view("sess1") or {}).get("numbers") or {})
        self.prompt("3: yes")
        self.assertEqual(self.sent(), [])

    def test_no_welcome_in_a_folder_left_off(self):
        ds.record_left_off([self.folder], "web-app")
        self.assert_no_welcome_after_a_gap()

    def test_no_welcome_in_a_private_folder(self):
        aid = "11111111-aaaa-bbbb-cccc-000000000001"
        ds.write_private(ds.conf_path("web-app"), json.dumps({"name": "web-app", "agent_id": aid, "workbook_id": WB,
                                                              "projects": [self.folder]}))
        self.assertTrue(ds.note_private({"agent_id": aid, "private": True}, aid))
        self.assertTrue(ds.private_here({"cwd": self.folder}))
        self.assert_no_welcome_after_a_gap()

    def test_no_welcome_in_a_folder_with_no_agent(self):
        self.folder = os.path.realpath(harness.tmpdir(self, prefix="noagent-", dir=os.path.dirname(self.folder)))
        os.environ["CLAUDE_PROJECT_DIR"] = self.folder
        self.assertEqual(ds.resolve(self.folder)[0], None)
        self.assert_no_welcome_after_a_gap()

    def test_no_welcome_in_a_headless_run(self):
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "sdk-cli"  # claude -p
        self.assert_no_welcome_after_a_gap()

    def test_the_list_is_blocking_first_and_short(self):
        t = ds.welcome_text(self.asks)
        lines = t.split("\n")
        self.assertEqual(lines[0], ds.WELCOME_HEAD)
        self.assertTrue(lines[1].startswith("  - **2. Beta**") and lines[2].startswith("  - **3. Gamma**"))
        self.assertTrue(lines[3].startswith("  - **1. Alpha**"))
        many = {"items": [dict(ASKS["items"][1], n=i) for i in range(1, 9)]}
        self.assertIn(f"  - 3 more: type **show asks**.", ds.welcome_text(many))
        self.assertIsNone(ds.welcome_text({"open": 0, "items": []}))
        self.assertIsNone(ds.welcome_text("datastash didn't answer just now"))

    def test_first_prompt_after_a_gap_opens_with_the_asks_and_they_answer(self):
        self.prompt("hello")
        self.assertEqual(self.reads, 0)  # the first prompt: the session start showed them
        turn = ds._turn_path("sess1")
        old = time.time() - ds.WELCOME_GAP - 5
        os.utime(turn, (old, old))
        self.prompt("back again")
        self.assertEqual(self.reads, 1)
        self.assertIn(ds.WELCOME_HEAD, self.last_hook["systemMessage"])
        self.assertIn("do not repeat the list", self.last_hook["hookSpecificOutput"]["additionalContext"])
        self.prompt("next thing")
        self.assertEqual(self.reads, 1)  # no gap: nothing again
        self.prompt("3: no")
        self.assertEqual([(b["instance"], b["answer"]) for b in self.sent()], [(I_P, "no")])

    def test_session_start_opens_with_the_asks(self):
        out = self.hook(ds.cmd_check, {"session_id": "sess1", "hook_event_name": "SessionStart", "cwd": self.folder})
        self.assertIn(ds.WELCOME_HEAD, out.get("systemMessage") or "")
        self.assertIn("welcome-back asks", out["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("**", out["systemMessage"])  # the plugin's own surface shows no markdown
        self.assertIn("3", ds.read_view("sess1")["numbers"])

    def test_nothing_open_says_nothing(self):
        self.asks = {"open": 0, "items": []}
        self.prompt("hello")
        old = time.time() - ds.WELCOME_GAP - 5
        os.utime(ds._turn_path("sess1"), (old, old))
        self.prompt("back")
        self.assertNotIn(ds.WELCOME_HEAD, (self.last_hook or {}).get("systemMessage") or "")


if __name__ == "__main__":
    unittest.main()
