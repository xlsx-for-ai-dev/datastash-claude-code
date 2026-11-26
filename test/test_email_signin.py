"""Offline tests for XLI-417 (owner 11-26, "the one sign in plan"; the command is `datastash sync <email>`): a computer login
that is not signed in signs in by one emailed code.
- A plain `datastash sync` on a login that is not signed in says the one line: Type **datastash sync you@email.com**.
- The person's typed `datastash sync <email>` (checked in the transcript) keeps the email, files this login's waiting
  request and asks datastash for one code bound to this session; their typed `datastash sync <code>` in the SAME session
  sends it. A line the model wrote (not the person's typed entry) sends nothing. Another session's code request is never
  used.
- The session start files (and refreshes, with its own device code) the waiting request when an email is kept; one set
  up with the agent (`datastash sync --waiting-for`) works the same and never replaces the person's typed one.
- /datastash:install and the session start never loop back to "type /datastash:install" while nothing here is signed in.
- One sign-in per OS user: two Claude settings folders under one login share it, and the second never asks again.
No network; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_email_signin.py"""
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_install import Base, ds  # noqa: E402
import harness  # noqa: E402

BIN = os.path.join(os.path.dirname(HERE), "plugins", "datastash", "scripts", "datastash")
FILES = ("LOGIN_FILE", "LOGIN_REFRESH_FILE", "LOGIN_ACCESS_FILE", "LOGIN_PENDING_FILE", "WAIT_EMAIL_FILE")
WAIT_DC = "dsdc_" + "w" * 43
CODE_DC = "dsdc_" + "c" * 43


class EmailBase(Base):
    def setUp(self):
        super().setUp()
        self.keep = {k: getattr(ds, k) for k in FILES + ("_ensure_poller", "CONSENT_PATH")}
        for k in FILES + ("CONSENT_PATH",):
            setattr(ds, k, os.path.join(ds.CONF, os.path.basename(getattr(ds, k))))
        self.polls = []
        ds._ensure_poller = lambda: self.polls.append(1) or True
        self.assertEqual(ds.signin_state().get("mode"), ds.SIGNIN_DEVICE)  # a fresh computer: decided at its first run
        os.makedirs(ds.STATE, exist_ok=True)
        self.answers[ds.EMAIL_WAIT_PATH] = (200, {"device_code": WAIT_DC, "refreshed": False, "expires_in": 86400, "interval": 5})
        self.answers[ds.EMAIL_START_PATH] = (200, {"device_code": CODE_DC, "expires_in": 600, "code_digits": 6, "message": "Check your email for a code."})
        self.answers[ds.EMAIL_CODE_PATH] = (200, {"signed_in": 3})

    def tearDown(self):
        for k, v in self.keep.items():
            setattr(ds, k, v)
        super().tearDown()

    def run_checks(self, start=0):
        for argv, _cwd in list(self.spawned[start:]):
            if len(argv) > 3 and argv[2] == "status-verify":
                ds.cmd_status_verify([argv[3]])

    def person_types(self, text, sid="s1", source="typed"):
        start = len(self.spawned)
        out, _pid = self.prompt(text, sid=sid, source=source)
        self.run_checks(start)
        return out

    def posts(self, path):
        return [c[3] for c in self.calls if c[0] == "POST" and c[1] == path]

    def ctx(self, out):
        return (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")

    def outcomes(self, sid="s1"):
        return ds.take_outcomes(sid)


class NotSignedIn(EmailBase):
    def test_plain_sync_says_the_one_line(self):
        out = self.person_types("datastash sync")
        self.assertIn("Type **datastash sync you@email.com** to sign in this computer.", self.ctx(out))
        self.assertEqual(self.posts(ds.EMAIL_START_PATH), [])

    def test_typed_email_files_the_waiting_request_and_asks_for_one_code_for_this_session(self):
        out = self.person_types("datastash sync Person@Example.com")
        self.assertIn(ds.CHECK_EMAIL_LINE, self.ctx(out))
        self.assertEqual(ds.waiting_email(), "person@example.com")
        wait = self.posts(ds.EMAIL_WAIT_PATH)
        self.assertEqual(len(wait), 1)
        self.assertEqual(wait[0]["email"], "person@example.com")
        start = self.posts(ds.EMAIL_START_PATH)
        self.assertEqual(len(start), 1)
        self.assertEqual(start[0]["email"], "person@example.com")
        self.assertEqual(start[0]["waiting_code"], WAIT_DC)  # this login is on the list
        self.assertEqual(ds._read_private_json(ds._code_req_path("s1"))["device_code"], CODE_DC)
        self.assertEqual(ds._email_pending()["device_code"], WAIT_DC)
        self.assertTrue(self.polls)  # its own poll waits for the key
        self.assertEqual(self.outcomes(), [])

    def test_the_code_in_the_same_session_signs_in(self):
        self.person_types("datastash sync person@example.com")
        self.person_types("datastash sync 482913")
        sent = self.posts(ds.EMAIL_CODE_PATH)
        self.assertEqual(sent, [{"device_code": CODE_DC, "code": "482913"}])
        self.assertEqual(self.outcomes(), ["Signed in: 3 computer logins. Setup finishes by itself; the datastash command works now."])
        self.assertIsNone(ds._read_private_json(ds._code_req_path("s1")))  # once

    def test_the_typed_code_sign_in_is_the_typed_sync(self):
        # x-live #531 (owner 11-26): the typed email then code IS the person's typed datastash sync: keeping, sync,
        # timers and session saving on here, so the install says "Synced..." with no connector call
        self.assertFalse(ds.sync_on())
        self.person_types("datastash sync person@example.com")
        self.assertFalse(ds.sync_on())  # the email alone turns nothing on
        self.person_types("datastash sync 482913")
        self.assertEqual(ds.keep_state(), "yes")
        self.assertTrue(ds.sync_on())
        self.assertIs(ds.timers_choice()["on"], True)
        self.assertIs(ds.sessions_on(), True)
        self.assertEqual(ds.onboard_install_lines({"session_id": "s1"}), [ds.SYNCED_LINE])

    def test_the_code_sign_in_never_changes_a_choice_made_here(self):
        ds.record_keep_all(False, "typed")
        self.person_types("datastash sync person@example.com")
        self.person_types("datastash sync 482913")
        self.assertEqual(ds.keep_state(), "no")
        self.assertFalse(ds.sync_on())
        ds.record_keep_all(True, "typed")
        ds.set_timers(False, "typed")
        ds.record_sessions(False)
        self.person_types("datastash sync person@example.com")
        self.person_types("datastash sync 482914")
        self.assertTrue(ds.sync_on())
        self.assertIs(ds.timers_choice()["on"], False)
        self.assertIs(ds.sessions_on(), False)

    def test_a_refused_or_agent_typed_code_turns_nothing_on(self):
        self.person_types("datastash sync person@example.com")
        self.answers[ds.EMAIL_CODE_PATH] = (400, {"error": "invalid_code", "message": "That code did not sign in."})
        self.person_types("datastash sync 000000")
        self.assertIsNone(ds.keep_state())
        self.assertFalse(ds.sync_on())
        self.answers[ds.EMAIL_CODE_PATH] = (200, {"signed_in": 1})
        self.person_types("datastash sync 482913", source="sdk")
        self.assertIsNone(ds.keep_state())
        self.assertIsNone(ds.sessions_on())

    def test_a_code_in_another_session_is_never_sent(self):
        self.person_types("datastash sync person@example.com", sid="s1")
        self.person_types("datastash sync 482913", sid="s2")
        self.assertEqual(self.posts(ds.EMAIL_CODE_PATH), [])
        self.assertEqual(self.outcomes("s2"), [ds.NO_CODE_ASKED_LINE])

    def test_a_wrong_code_shows_datastash_refusal(self):
        self.person_types("datastash sync person@example.com")
        self.answers[ds.EMAIL_CODE_PATH] = (400, {"error": "invalid_code", "message": "That code did not sign in."})
        self.person_types("datastash sync 000000")
        self.assertEqual(len(self.outcomes()), 1)
        self.assertIsNotNone(ds._read_private_json(ds._code_req_path("s1")))  # kept: the person may retype it

    def test_agent_typed_lines_send_nothing(self):
        self.person_types("datastash sync person@example.com", source="sdk")
        self.assertEqual(self.posts(ds.EMAIL_START_PATH), [])
        self.assertIsNone(ds.waiting_email())
        ds.write_private(ds._code_req_path("s1"), json.dumps({"device_code": CODE_DC, "until": 4102444800}))
        self.person_types("datastash sync 482913", source="sdk")
        self.assertEqual(self.posts(ds.EMAIL_CODE_PATH), [])

    def test_install_says_the_sign_in_line_never_install_again(self):
        v, _pid = self.slash("/datastash:install")
        ai = self.ctx(v)
        self.assertIn(ds.SIGN_IN_INSTALL_LINE, ai)  # the fallback when no connector tool is there
        self.assertNotIn(ds.NO_CONNECTOR_LINE, ai)
        self.assertNotIn("/datastash:install", ds.SIGN_IN_INSTALL_LINE)
        self.assertNotIn("/datastash:install", ds.notice_text("noagent"))

    def test_session_start_files_and_refreshes_the_waiting_request(self):
        ds.set_waiting_email("person@example.com", "typed")
        person, ai = ds.email_session_note({"session_id": "s1", "cwd": self.folder})
        self.assertIn("datastash sync person@example.com", person)
        self.assertIn("not signed in", ai)
        self.assertEqual(len(self.posts(ds.EMAIL_WAIT_PATH)), 1)
        self.assertNotIn("device_code", self.posts(ds.EMAIL_WAIT_PATH)[0])
        ds.email_session_note({"session_id": "s2", "cwd": self.folder})
        self.assertEqual(self.posts(ds.EMAIL_WAIT_PATH)[1]["device_code"], WAIT_DC)  # refreshed, same request
        self.assertEqual(ds.email_session_note({"session_id": "s2", "cwd": self.folder}), (None, None))  # once a session

    def test_session_start_without_an_email_files_nothing_and_says_the_line(self):
        person, _ai = ds.email_session_note({"session_id": "s1", "cwd": self.folder})
        self.assertEqual(person, ds._plain_typed(ds.SIGN_IN_TYPE_LINE))
        self.assertEqual(self.posts(ds.EMAIL_WAIT_PATH), [])

    def test_an_agent_set_email_files_a_waiting_request_but_never_replaces_a_typed_one(self):
        with self.assertRaises(SystemExit):
            ds.cmd_sync(["--waiting-for", "not-an-email"])
        ds.cmd_sync(["--waiting-for", "agent@example.com"])
        self.assertEqual(ds.waiting_email(), "agent@example.com")
        self.assertEqual(self.posts(ds.EMAIL_WAIT_PATH)[0]["email"], "agent@example.com")
        ds.set_waiting_email("person@example.com", "typed")
        ds.cmd_sync(["--waiting-for", "agent@example.com"])
        self.assertEqual(ds.waiting_email(), "person@example.com")

    def test_a_waiting_request_poll_keeps_its_request_until_it_ends(self):
        ds.write_private(ds.LOGIN_PENDING_FILE, json.dumps({"kind": "email", "device_code": WAIT_DC, "interval": 5, "until": 0}))
        self.assertEqual(ds._login_poll_loop(ds._email_pending(), 5, 0), 0)
        self.assertIsNotNone(ds._email_pending())  # never dropped while it waits


class SignedIn(EmailBase):
    def setUp(self):
        super().setUp()
        ds.write_private(ds.LOGIN_REFRESH_FILE, "dsrt_" + "r" * 43 + "\n")
        ds.write_private(ds.LOGIN_FILE, json.dumps({"store": "file", "at": 1}))

    def test_a_signed_in_session_asks_for_a_code_for_the_others_without_a_waiting_request(self):
        self.person_types("datastash sync person@example.com")
        self.assertEqual(self.posts(ds.EMAIL_WAIT_PATH), [])
        start = self.posts(ds.EMAIL_START_PATH)
        self.assertEqual(len(start), 1)
        self.assertNotIn("waiting_code", start[0])
        self.assertIsNone(ds.waiting_email())  # a signed-in login keeps no waiting email

    def test_plain_sync_after_the_code_sign_in_says_synced_with_no_connector_call(self):
        # x-live #531: on the login the code sign-in already was the typed sync; a session signed in here has no
        # connector tools, so the plugin gives the Synced line itself and starts no check
        ds.login_typed_sync()
        start = len(self.spawned)
        out, _pid = self.prompt("datastash sync", sid="s1")
        ai = self.ctx(out)
        self.assertIn(ds.SYNCED_LINE, ai)
        self.assertNotIn("`sync` tool", ai)
        self.assertEqual(self.spawned[start:], [])
        self.assertEqual(ds.onboard_state("s1").get("stage"), "status")

    def test_plain_sync_while_sync_is_off_here_still_goes_through_the_typed_check(self):
        out, _pid = self.prompt("datastash sync", sid="s1")
        self.assertEqual(self.ctx(out), ds.SYNC_TYPED_AI)

    def test_no_sign_in_line_at_session_start(self):
        self.assertEqual(ds.email_session_note({"session_id": "s1", "cwd": self.folder}), (None, None))


class OneSignInPerLogin(unittest.TestCase):
    def test_two_claude_settings_folders_under_one_login_share_one_sign_in(self):
        home = harness.tmpdir(self, prefix="login-", dir=harness.ROOT)
        conf = os.path.join(home, ".config", "datastash")
        os.makedirs(conf, mode=0o700)
        for name, text in (("login.refresh", "dsrt_" + "r" * 43 + "\n"), ("login.json", json.dumps({"store": "file", "at": 1}))):
            p = os.path.join(conf, name)
            with open(os.open(p, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
                f.write(text)
        for plan in ("claude-plan-a", "claude-plan-b"):
            env = dict(os.environ, HOME=home, DATASTASH_CONFIG_DIR=conf, CLAUDE_CONFIG_DIR=os.path.join(home, plan), DATASTASH_NO_NETWORK="1")
            r = subprocess.run([sys.executable, "-I", BIN, "login", "--check"], capture_output=True, text=True, env=env, cwd=home, timeout=60)
            self.assertIn("signed in: yes", r.stdout, plan)

    def test_the_sign_in_files_never_depend_on_the_claude_settings_folder(self):
        for k in FILES:
            self.assertNotIn(".claude", getattr(ds, k))


class Phrases(unittest.TestCase):
    def test_email_and_code(self):
        self.assertEqual(ds.sync_email_of("datastash sync You@Example.COM"), "you@example.com")
        self.assertEqual(ds.sync_email_of("  Datastash sync you@example.com.  "), "you@example.com")
        self.assertIsNone(ds.sync_email_of("datastash sync"))
        self.assertIsNone(ds.sync_email_of("please datastash sync you@example.com"))
        self.assertEqual(ds.sync_code_of("datastash sync 048213"), "048213")
        self.assertIsNone(ds.sync_code_of("datastash sync 48213"))
        self.assertIsNone(ds.sync_code_of("datastash sync 048-213"))


if __name__ == "__main__":
    unittest.main()
