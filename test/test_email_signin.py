#!/usr/bin/env python3
"""Offline tests for card 1 (owner 11-27, plugin 1.0.23): the simple email sign-in for one computer and this folder.
- /datastash:install on a computer that is not signed in: one line, "Type datastash sync you@email.com", nothing tried.
- `datastash sync you@email.com`: once the transcript shows the person typed it, this computer's own request and one
  code request (bound to this session); the model only says to check the email.
- `datastash sync 482913`: the code with this session's request, this computer's own login picked up and saved, then
  this folder's agent registered on it, its key and answer key picked up (bound pickup, JSON), saved, and "Done."
- A name datastash says is taken: one plain line. A line not typed by the person: nothing is sent.
- On that login, /datastash:install NAME and /datastash:uninstall NAME run on the login's REST routes, signed by an
  answer key here, with no connector.
No network; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_email_signin.py"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_install import ANSWER, KEY, LIST_ID, OWN, WB, Base, proof_ok, ds  # noqa: E402

EMAIL = "tester@example.com"
WAITING, CODE_REQ = "dsdc_" + "w" * 40, "dsdc_" + "c" * 40
REFRESH, ACCESS = "dsrt_" + "r" * 40, "dsat_" + "a" * 40
LOGIN_ANSWER, GRANT = "dsak_" + "g" * 43, "99999999-8888-7777-6666-555555555555"
FILES = ("LOGIN_FILE", "LOGIN_REFRESH_FILE", "LOGIN_ACCESS_FILE", "LOGIN_LOCK_FILE", "EMAIL_LOGIN_FILE")


class EmailSignIn(Base):
    def setUp(self):
        super().setUp()
        self.saved_files = {k: getattr(ds, k) for k in FILES + ("VERIFY_WAIT", "_keychain_set", "setup_row_kick")}
        for k in FILES:
            setattr(ds, k, os.path.join(ds.CONF, os.path.basename(getattr(ds, k))))
        ds.VERIFY_WAIT = 3
        ds._keychain_set = lambda token: False  # the refresh token goes to the mode-600 file here
        ds.setup_row_kick = lambda agent, here: None
        ds.computer_signed_in = lambda deep=False: None  # a fresh computer: no sign-in known here
        self.accepts = []
        inner = ds.http

        def http(method, path, key=None, body=None, form=None, timeout=40, accept="application/json"):
            self.accepts.append((path, accept, form))
            return inner(method, path, key=key, body=body, form=form, timeout=timeout, accept=accept)
        ds.http = http
        self.answers.update({
            ds.EMAIL_WAIT_PATH: (200, {"device_code": WAITING, "interval": 5, "expires_in": 86400}),
            ds.EMAIL_START_PATH: (200, {"device_code": CODE_REQ, "message": "Check your email"}),
            ds.EMAIL_CODE_PATH: (200, {"signed_in": 1}),
            ds.DEVICE_TOKEN_PATH: (200, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 900,
                                         "answer_key": LOGIN_ANSWER, "grant_id": GRANT}),
            ds.AGENT_REGISTER_PATH: (200, {"agent_id": OWN, "name": "web-app", "notes_tabs": [{"workbook_id": WB}]}),
            ds.AGENT_KEY_PATH: (200, {"pickup_code": "pickup-code-123456"}),
            ds.PICKUP_PATH: (200, {"agent_id": OWN, "name": "web-app", "key": KEY, "answer_key": ANSWER}),
        })

    def tearDown(self):
        for k, v in self.saved_files.items():
            setattr(ds, k, v)
        super().tearDown()

    def check(self, pid):
        """The checker the prompt hook started (detached in real use), run here."""
        self.assertTrue(any(a[0][-1] == pid for a in self.spawned), "the prompt hook started no checker")
        self.assertEqual(ds.cmd_status_verify([pid]), 0)

    def stop(self, sid="s1"):
        return self.hook(ds.cmd_status_check, {"session_id": sid, "hook_event_name": "Stop", "cwd": self.folder})

    def paths(self):
        return [c[1] for c in self.calls]

    def signed_in_by_code(self):
        _v, pid = self.prompt(f"datastash sync {EMAIL}")
        self.check(pid)
        v, pid = self.prompt("datastash sync 482913")
        self.check(pid)
        return v

    def test_install_not_signed_in_says_the_one_line_and_tries_nothing(self):
        v, _pid = self.slash("/datastash:install")
        self.assertEqual(v["decision"], "block")
        self.assertEqual(v["reason"], 'Type "datastash sync you@email.com"')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.spawned, [])

    def test_step_one_the_typed_email_asks_for_one_code_for_this_computer(self):
        v, pid = self.prompt(f"datastash sync {EMAIL}")
        ai = v["hookSpecificOutput"]["additionalContext"]
        self.assertIn(ds.CHECK_EMAIL_LINE, ai)
        self.assertNotIn(EMAIL, ai)
        self.assertEqual(self.calls, [])  # nothing goes out before the transcript shows the person typed it
        self.check(pid)
        self.assertEqual(self.paths(), [ds.EMAIL_WAIT_PATH, ds.EMAIL_START_PATH])
        start = self.calls[1][3]
        self.assertEqual((start["email"], start["waiting_code"]), (EMAIL, WAITING))
        self.assertEqual(ds._read_private_json(ds._code_req_path("s1"))["device_code"], CODE_REQ)
        self.assertIsNone(self.stop())  # all went well: no extra line

    def test_steps_two_and_three_the_code_signs_in_registers_this_folder_and_says_done(self):
        v = self.signed_in_by_code()
        self.assertIn("Checking the code.", v["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.paths()[2:], [ds.EMAIL_CODE_PATH, ds.DEVICE_TOKEN_PATH, ds.AGENT_REGISTER_PATH,
                                            ds.AGENT_KEY_PATH, ds.PICKUP_PATH])
        self.assertEqual(self.calls[2][3], {"device_code": CODE_REQ, "code": "482913"})
        self.assertEqual(self.calls[3][3], {"device_code": WAITING})
        self.assertEqual(self.calls[4][3]["name"], "web-app")
        key_call = self.calls[5][3]
        self.assertTrue(key_call["only_if_no_key"])
        pickup = [a for a in self.accepts if a[0] == ds.PICKUP_PATH][0]
        self.assertEqual(pickup[1], "application/json")
        self.assertEqual(ds.challenge_of(pickup[2]["verifier"]), key_call["code_challenge"])
        self.assertTrue(ds.login_present() and ds.email_login())
        self.assertEqual(ds.read_key("web-app"), KEY)
        self.assertEqual(ds.read_agent_answer("web-app"), ANSWER)
        self.assertEqual(ds.read_json(ds.conf_path("web-app"), {}).get("workbook_id"), WB)
        self.assertEqual(self.stop()["systemMessage"], "datastash: Done.")
        self.assertFalse(os.path.exists(ds._code_req_path("s1")))  # the code request is spent

    def test_name_taken_is_one_plain_line(self):
        self.answers[ds.AGENT_REGISTER_PATH] = (409, {"code": "agent_name_taken"})
        self.signed_in_by_code()
        self.assertEqual(self.stop()["systemMessage"], "datastash: " + ds.NAME_TAKEN_LINE)
        self.assertEqual(ds.NAME_TAKEN_LINE, "That name is taken. Type /datastash:install NEWNAME.")
        self.assertIsNone(ds.read_key("web-app"))
        self.assertTrue(ds.email_login())  # the computer is signed in; only the name needs a retry

    def test_a_wrong_code_is_refused_plainly_and_signs_nothing_in(self):
        self.answers[ds.EMAIL_CODE_PATH] = (400, {"error": "invalid_code"})
        self.signed_in_by_code()
        self.assertEqual(self.stop()["systemMessage"], "datastash: " + ds.BAD_CODE_LINE)
        self.assertNotIn(ds.DEVICE_TOKEN_PATH, self.paths())
        self.assertFalse(ds.login_present())

    def test_a_code_with_no_code_asked_in_this_session_sends_nothing(self):
        _v, pid = self.prompt("datastash sync 482913")
        self.check(pid)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.stop()["systemMessage"], "datastash: " + ds.NO_CODE_ASKED_LINE)

    def test_lines_not_typed_by_the_person_send_nothing(self):
        _v, pid = self.prompt(f"datastash sync {EMAIL}", source="sdk")
        self.check(pid)
        self.assertEqual(self.calls, [])

    def test_install_again_with_a_new_name_on_the_login(self):
        self.answers[ds.AGENT_REGISTER_PATH] = (409, {"code": "agent_name_taken"})
        self.signed_in_by_code()
        self.stop()
        self.answers[ds.AGENT_REGISTER_PATH] = (200, {"agent_id": OWN, "name": "web-app-b", "notes_tabs": [{"workbook_id": WB}]})
        self.answers[ds.PICKUP_PATH] = (200, {"agent_id": OWN, "name": "web-app-b", "key": KEY, "answer_key": ANSWER})
        v, pid = self.slash("/datastash:install web-app-b")
        self.assertIn("Setting up.", v["hookSpecificOutput"]["additionalContext"])
        self.check(pid)
        self.assertEqual(self.calls[-3][3]["name"], "web-app-b")
        self.assertEqual(self.stop()["systemMessage"], "datastash: Done.")
        self.assertEqual(ds.read_key("web-app-b"), KEY)

    def test_uninstall_on_the_login_is_signed_here_and_sent_to_the_login(self):
        self.signed_in_by_code()
        self.stop()
        self.answers[ds.AGENTS_LIST_PATH] = (200, {"list_id": LIST_ID, "expires_in_seconds": 600,
                                                   "agents": [{"agent_id": OWN, "name": "web-app", "visibility": "public"}]})
        self.answers[ds.AGENTS_NOTE_PATH] = (200, {"run_id": None, "kind": "uninstall", "targets": [OWN],
                                                   "removed": ["web-app"], "this_agent_last": True})
        self.answers[ds.UNINSTALL_SELF_PATH] = (200, {})
        v, pid = self.slash("/datastash:uninstall web-app")
        self.assertIn("Checking.", v["hookSpecificOutput"]["additionalContext"])
        self.check(pid)
        note = [c for c in self.calls if c[1] == ds.AGENTS_NOTE_PATH][0]
        self.assertEqual(note[2], ACCESS)  # on the login, not a connector
        self.assertEqual((note[3]["kind"], note[3]["targets"]), ("uninstall", [OWN]))
        self.assertTrue(proof_ok(note[3]["typed_proof"], "uninstall", [OWN], pid, "/datastash:uninstall web-app"))
        self.assertIn(ds.UNINSTALL_SELF_PATH, self.paths())
        self.assertIn("web-app", self.stop()["systemMessage"])


if __name__ == "__main__":
    unittest.main()
