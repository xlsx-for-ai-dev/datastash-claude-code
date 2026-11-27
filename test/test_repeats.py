#!/usr/bin/env python3
"""Offline tests for XLI-413 repeats (plugin 1.0.24): `datastash repeat[s] ...`, the person's repeat questions, ONE list
per account shared by every agent (GET/POST /api/v1/agent/repeats with the folder's own agent key).
- `repeat` and `repeats` are the same word, as the command and before every action; small typos still work (repaet add,
  repeats ad, remvoe); "who are you" is never read as "show".
- list (--for AGENT), add (--answer), show N|QUESTION, answer N TEXT, part N TEXT, remove N|QUESTION: each one call with
  the right path and body; bad arguments die with the usage line and send nothing.
- datastash's refusal is printed as untrusted data and exits 1, as `datastash api` does.
- The session-start checklist and the skill tell the agent to answer listed repeats from the list.
No network, no key; under test/harness.py.
Run: python3 -I test/test_repeats.py"""
import io
import json
import os
import sys
import unittest
import urllib.parse
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import ds, TMP  # noqa: E402  (the same harness and plugin copy)
import harness  # noqa: E402

WB = "0b6a3f9e-1c2d-4e5f-8a9b-0c1d2e3f4a6e"
KEY = "ldgr_ag_repeatstestkeyrepeatstestkey"
SLUG = "repeats-agent"
PATH = "/api/v1/agent/repeats"


class RepeatBase(unittest.TestCase):
    def setUp(self):
        self.folder = os.path.realpath(harness.tmpdir(self, prefix="proj-", dir=TMP))
        ds.ensure_dirs()
        ds.write_private(ds.conf_path(SLUG), json.dumps({"name": SLUG, "workbook_id": WB, "projects": [self.folder]}))
        self.calls = []
        self.refuse = None

        def fake_http(method, path, key=None, body=None, form=None, timeout=40, **kw):
            self.calls.append((method, path, key, json.loads(json.dumps(body))))
            if self.refuse is not None:
                raise ds.ApiError(*self.refuse)
            return 200, "application/json", json.dumps({"ok": True, "repeats": [], "suggestions": ["who are you"]})
        self.saved = {}
        for name, new in (("http", fake_http), ("read_key", lambda s: KEY), ("auto_setup_check", lambda *a, **k: None),
                          ("spawn_detached", lambda argv, cwd: None)):
            self.saved[name] = getattr(ds, name)
            setattr(ds, name, new)
        self.env = {k: os.environ.get(k) for k in ("CLAUDE_PROJECT_DIR",)}
        os.environ["CLAUDE_PROJECT_DIR"] = self.folder
        self.addCleanup(self.clear_outbox)

    def clear_outbox(self):
        d = ds.outbox_dir(SLUG)
        for n in (os.listdir(d) if os.path.isdir(d) else []):
            os.unlink(os.path.join(d, n))  # test code clearing its own throwaway folder

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(ds, k, v)
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def run_cli(self, *argv):
        saved = sys.argv
        sys.argv = ["datastash", *argv]
        try:
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                try:
                    code = ds.main()
                except SystemExit as e:
                    code = e.code
        finally:
            sys.argv = saved
        return code, out.getvalue(), err.getvalue()

    def one(self):
        self.assertEqual(len(self.calls), 1, self.calls)
        method, path, key, body = self.calls[0]
        self.assertEqual(key, KEY)
        base, _, q = path.partition("?")
        return method, base, dict(urllib.parse.parse_qsl(q)), body


class Words(unittest.TestCase):
    def test_repeat_and_repeats_and_small_typos_are_one_word(self):
        for w in ("repeat", "repeats", "Repeats", "REPEAT", "repaet", "repeta", "repeast", "repat", "repeatss"):
            self.assertEqual(ds.repeat_word(w), "repeat", w)
        for w in ("reply", "read", "remember", "rep", "status", "", "re-peat"):
            self.assertIsNone(ds.repeat_word(w), w)

    def test_actions_singular_plural_and_typos(self):
        cases = {"add": "add", "adds": "add", "ad": "add", "list": "list", "lists": "list", "ls": "list",
                 "remove": "remove", "removes": "remove", "remvoe": "remove", "rm": "remove", "delete": "remove",
                 "answer": "answer", "answers": "answer", "anwser": "answer", "part": "part", "parts": "part",
                 "show": "show", "get": "show"}
        for w, want in cases.items():
            self.assertEqual(ds.repeat_action(w), want, w)
        for w in ("who", "where", "what", "did", "why", "2"):
            self.assertIsNone(ds.repeat_action(w), w)


class Sends(RepeatBase):
    def test_plain_repeat_and_repeats_list(self):
        for word in ("repeat", "repeats", "repaets"):
            self.calls.clear()
            code, out, _ = self.run_cli(word)
            self.assertEqual(code, 0, word)
            self.assertEqual(self.one(), ("GET", PATH, {}, None))
            self.assertIn("<<datastash-data", out)
        self.calls.clear()
        self.run_cli("repeats", "list", "--for", "Builder")
        self.assertEqual(self.one(), ("GET", PATH, {"agent": "Builder"}, None))

    def test_add_with_either_word_and_an_answer(self):
        code, _, _ = self.run_cli("repeat", "add", "who", "are", "you?")
        self.assertEqual(code, 0)
        self.assertEqual(self.one(), ("POST", PATH, {}, {"question": "who are you?"}))
        self.calls.clear()
        self.run_cli("repeats", "ad", "where are we", "--answer", "Building repeats.")
        self.assertEqual(self.one(), ("POST", PATH, {}, {"question": "where are we", "answer": "Building repeats."}))

    def test_show_by_number_or_question_for_one_agent(self):
        self.run_cli("repeat", "show", "2", "--for", "Reviewer")
        self.assertEqual(self.one(), ("GET", PATH, {"n": "2", "agent": "Reviewer"}, None))
        self.calls.clear()
        self.run_cli("repeats", "who", "are", "you")  # not an action: that one repeat, by its question
        self.assertEqual(self.one(), ("GET", PATH, {"question": "who are you"}, None))
        self.calls.clear()
        self.run_cli("repeat", "#3")
        self.assertEqual(self.one(), ("GET", PATH, {"n": "3"}, None))

    def test_answer_part_and_remove(self):
        self.run_cli("repeat", "answer", "1", "Two", "agents", "work", "here.")
        self.assertEqual(self.one(), ("POST", PATH + "/answer", {}, {"n": 1, "answer": "Two agents work here."}))
        self.calls.clear()
        self.run_cli("repeats", "part", "1", "I build the server.")
        self.assertEqual(self.one(), ("POST", PATH + "/part", {}, {"n": 1, "part": "I build the server."}))
        self.calls.clear()
        self.run_cli("repeat", "part", "1")  # no text: clears this agent's own part
        self.assertEqual(self.one(), ("POST", PATH + "/part", {}, {"n": 1, "part": ""}))
        self.calls.clear()
        self.run_cli("repeats", "remove", "2")
        self.assertEqual(self.one(), ("POST", PATH + "/remove", {}, {"n": 2}))
        self.calls.clear()
        self.run_cli("repeat", "remvoe", "where are we")
        self.assertEqual(self.one(), ("POST", PATH + "/remove", {}, {"question": "where are we"}))


class Refuses(RepeatBase):
    def test_bad_arguments_send_nothing(self):
        for argv in (("repeat", "add"), ("repeat", "remove"), ("repeat", "answer"), ("repeat", "part", "x", "text"),
                     ("repeat", "answer", "0", "text"), ("repeats", "list", "extra"), ("repeat", "add", "q", "--answer"),
                     ("repeat", "show", "1", "--for")):
            code, _, err = self.run_cli(*argv)
            self.assertNotEqual(code, 0, argv)
            self.assertIn(ds.REPEAT_USAGE, err, argv)
        self.assertEqual(self.calls, [])

    def test_a_refusal_is_printed_as_data(self):
        self.refuse = (404, {"code": "not_found", "message": "No such repeat on this account."})
        code, out, _ = self.run_cli("repeat", "remove", "9")
        self.assertEqual(code, 1)
        self.assertIn("not_found", out)
        self.assertIn("HTTP 404", out)
        self.assertEqual(ds.outbox_entries(SLUG), [])

    def test_no_agent_here_sends_nothing(self):
        os.environ["CLAUDE_PROJECT_DIR"] = os.path.realpath(harness.tmpdir(self, prefix="none-", dir=TMP))
        code, _, err = self.run_cli("repeats")
        self.assertNotEqual(code, 0)
        self.assertIn("no agent is linked", err)
        self.assertEqual(self.calls, [])


class Guidance(unittest.TestCase):
    def test_the_checklist_and_the_skill_say_to_answer_from_the_list(self):
        items = [i for _h, its in ds.FULL_DDS for i in its]
        line = [i for i in items if i[0] == "C22"]
        self.assertEqual(len(line), 1)
        self.assertIn("repeats", line[0][1])
        self.assertIn("1.0.24", ds.WHATS_NEW)
        skill = open(os.path.join(ds.PLUGIN_ROOT, "skills", "datastash-agent", "SKILL.md")).read()
        self.assertIn("datastash repeat add", skill)
        self.assertIn("answer at once from its stored answer", skill)


if __name__ == "__main__":
    unittest.main()
