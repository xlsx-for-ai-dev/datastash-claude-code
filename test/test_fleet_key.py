#!/usr/bin/env python3
"""Offline tests for XLI-419 (plugin 1.0.24): the fleet key.
- `datastash fleet-key create NAME [--expires 1d]`, typed by the person on a signed-in computer: nothing goes out until
  the checker finds it typed by the person; then one create call with a typed proof signed by this computer's login
  answer key (bound to the name and the expiry); the key is shown to the person once (the Stop hook's line, never the
  model's context). A line not typed by the person sends nothing; a machine that itself joined with a fleet key can't.
- `datastash fleet-key list` and `datastash fleet-key revoke NAME`: the list and the revoke on the login.
- Session start on a machine with no login and a fleet key (DATASTASH_FLEET_KEY or the mode-600 file): it joins by
  itself (its own login saved here), then this folder's agent is set up on that login in the background. A file others
  can read is not used; a refused key is said once and not tried again at every start.
No network; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_fleet_key.py"""
import hashlib
import hmac
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_install import ANSWER, KEY, OWN, WB, Base, ds  # noqa: E402

REFRESH, ACCESS = "dsrt_" + "r" * 40, "dsat_" + "a" * 40
LOGIN_ANSWER, GRANT = "dsak_" + "g" * 43, "99999999-8888-7777-6666-555555555555"
FLEET_KEY = "dsfk_" + "F" * 43
FILES = ("LOGIN_FILE", "LOGIN_REFRESH_FILE", "LOGIN_ACCESS_FILE", "LOGIN_LOCK_FILE", "EMAIL_LOGIN_FILE", "FLEET_LOGIN_FILE",
         "FLEET_KEY_FILE")


class FleetKey(Base):
    def setUp(self):
        super().setUp()
        # XLI-441: added to Base's saved setup (never in place of it), so Base.tearDown still puts back _temp_roots,
        # CONF and STATE for the test files run after this one (test_setup.py's TempAndSystem)
        for k in FILES + ("VERIFY_WAIT", "_keychain_set", "setup_row_kick", "computer_signed_in"):
            self.saved.setdefault(k, getattr(ds, k))
        for k in FILES:
            setattr(ds, k, os.path.join(ds.CONF, os.path.basename(getattr(ds, k))))
        ds.VERIFY_WAIT = 3
        ds._keychain_set = lambda token: False
        ds.setup_row_kick = lambda agent, here: None
        ds.computer_signed_in = lambda deep=False: None
        self.saved_env = os.environ.pop("DATASTASH_FLEET_KEY", None)
        try:
            with open(ds.CONSENT_PATH, "rb") as f:
                self.saved_consent = f.read()
        except OSError:
            self.saved_consent = None
        self.answers.update({
            ds.FLEET_CREATE_PATH: (200, {"name": "my-fleet", "fleet_key": FLEET_KEY, "expires_at": "2026-11-28T15:00:00Z"}),
            ds.FLEET_JOIN_PATH: (200, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 900,
                                       "answer_key": LOGIN_ANSWER, "grant_id": GRANT, "fleet": "my-fleet"}),
            ds.FLEET_REVOKE_PATH: (200, {"name": "my-fleet", "revoked": True, "signed_out": 2}),
            ds.FLEET_LIST_PATH: (200, {"fleet_keys": [{"name": "my-fleet", "expires_at": "2026-11-28T15:00:00Z",
                                                       "expired": False, "revoked_at": None, "machines": [
                                                           {"device_name": "box-1 / agent", "signed_in": True},
                                                           {"device_name": "box-2 / agent", "signed_in": False}]}]}),
            ds.AGENT_REGISTER_PATH: (200, {"agent_id": OWN, "name": "web-app", "notes_tabs": [{"workbook_id": WB}]}),
            ds.AGENT_KEY_PATH: (200, {"pickup_code": "pickup-code-123456"}),
            ds.PICKUP_PATH: (200, {"agent_id": OWN, "name": "web-app", "key": KEY, "answer_key": ANSWER}),
        })

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(ds, k, v)
        os.environ.pop("DATASTASH_FLEET_KEY", None)
        if self.saved_env is not None:
            os.environ["DATASTASH_FLEET_KEY"] = self.saved_env
        try:
            if self.saved_consent is None:
                os.remove(ds.CONSENT_PATH)
            else:
                with open(ds.CONSENT_PATH, "wb") as f:
                    f.write(self.saved_consent)
        except OSError:
            pass
        super().tearDown()

    # --- helpers
    def signed_in_person(self):
        """This computer signed in (the person's own login, emailed code) with this folder's agent keyed."""
        ds._save_login(REFRESH, ACCESS, 900, LOGIN_ANSWER, GRANT)
        ds.write_private(ds.EMAIL_LOGIN_FILE, json.dumps({"at": 1}))
        ds.save_agent("web-app", OWN, KEY, WB, self.folder)

    def check(self, pid):
        self.assertTrue(any(a[0][-1] == pid for a in self.spawned), "the prompt hook started no checker")
        self.assertEqual(ds.cmd_status_verify([pid]), 0)

    def stop(self, sid="s1"):
        return self.hook(ds.cmd_status_check, {"session_id": sid, "hook_event_name": "Stop", "cwd": self.folder})

    def paths(self):
        return [c[1] for c in self.calls]

    def mac_ok(self, proof, action, pid, typed):
        lines = [ds.TYPED_SYNC_LABEL, f"grant:{GRANT}", OWN.lower(), action, pid,
                 hashlib.sha256(typed.encode()).hexdigest(), str(proof["typed_at"]), str(proof["iat"])]
        want = hmac.new(LOGIN_ANSWER.encode(), "\n".join(lines).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(want, proof["mac"])

    # --- the typed lines
    def test_parse_the_typed_lines(self):
        self.assertEqual(ds.fleet_prompt_of("datastash fleet-key create my-fleet"), ("create", "my-fleet", None))
        self.assertEqual(ds.fleet_prompt_of("datastash fleet-key create my-fleet --expires 1d"), ("create", "my-fleet", 86400))
        self.assertEqual(ds.fleet_prompt_of("datastash fleet-key create f2 --expires=12h"), ("create", "f2", 43200))
        self.assertEqual(ds.fleet_prompt_of("datastash fleet-key list"), ("list",))
        self.assertEqual(ds.fleet_prompt_of("datastash fleet-key revoke my-fleet"), ("revoke", "my-fleet"))
        for bad in ("datastash fleet-key create", "datastash fleet-key create My Fleet", "datastash fleet-key create x --expires soon",
                    "datastash fleet-key create x --expires 10s", "datastash fleet-key list all"):
            self.assertEqual(ds.fleet_prompt_of(bad), "usage", bad)
        self.assertIsNone(ds.fleet_prompt_of("please make a fleet key"))

    def test_create_sends_nothing_until_typed_then_shows_the_key_once_to_the_person(self):
        self.signed_in_person()
        text = "datastash fleet-key create my-fleet --expires 1d"
        v, pid = self.prompt(text)
        ai = v["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Checking.", ai)
        self.assertEqual(self.calls, [])
        self.check(pid)
        create = [c for c in self.calls if c[1] == ds.FLEET_CREATE_PATH]
        self.assertEqual(len(create), 1)
        _m, _p, tok, body = create[0]
        self.assertEqual(tok, ACCESS)
        self.assertEqual((body["name"], body["expires_in_seconds"]), ("my-fleet", 86400))
        self.assertTrue(self.mac_ok(body["typed_proof"], "fleet_key_create:my-fleet:86400", pid, text))
        msg = self.stop()["systemMessage"]
        self.assertIn(FLEET_KEY, msg)  # the person sees it once
        self.assertNotIn(FLEET_KEY, json.dumps(v))  # never in the model's context
        self.assertIsNone(self.stop())  # once

    def test_create_not_typed_by_the_person_sends_nothing(self):
        self.signed_in_person()
        _v, pid = self.prompt("datastash fleet-key create my-fleet", source="sdk")
        self.check(pid)
        self.assertNotIn(ds.FLEET_CREATE_PATH, self.paths())

    def test_create_on_a_computer_not_signed_in_or_on_a_fleet_machine_is_refused(self):
        v, _pid = self.prompt("datastash fleet-key create my-fleet")
        self.assertEqual(v.get("decision"), "block")
        self.assertIn("not signed in", v["reason"])
        self.signed_in_person()
        ds.write_private(ds.FLEET_LOGIN_FILE, json.dumps({"fleet": "other", "at": 1}))
        v, _pid = self.prompt("datastash fleet-key create my-fleet", sid="s2")
        self.assertEqual(v.get("decision"), "block")
        self.assertEqual(self.calls, [])

    def test_list_and_revoke(self):
        self.signed_in_person()
        _v, pid = self.prompt("datastash fleet-key list")
        self.check(pid)
        msg = self.stop()["systemMessage"]
        self.assertIn("my-fleet", msg)
        self.assertIn("1 of 2 machines signed in", msg)
        _v, pid = self.prompt("datastash fleet-key revoke my-fleet")
        self.check(pid)
        rev = [c for c in self.calls if c[1] == ds.FLEET_REVOKE_PATH][0]
        self.assertEqual(rev[3], {"name": "my-fleet"})
        self.assertIn("2 machines signed out", self.stop()["systemMessage"])

    def test_the_terminal_command_does_nothing(self):
        self.assertEqual(ds.cmd_fleet_key(["create", "x"]), 2)
        self.assertEqual(self.calls, [])

    # --- the machine's join at session start
    def test_a_machine_with_the_key_file_joins_by_itself_and_sets_this_folder_up(self):
        ds.ensure_dirs()
        fd = os.open(ds.FLEET_KEY_FILE, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, (FLEET_KEY + "\n").encode())
        os.close(fd)
        line = ds.fleet_join()
        self.assertEqual(line, ds.FLEET_JOINED_LINE.format(fleet="my-fleet"))
        join = [c for c in self.calls if c[1] == ds.FLEET_JOIN_PATH][0]
        self.assertEqual(join[3]["fleet_key"], FLEET_KEY)
        self.assertIsNone(join[2])  # no other credential
        self.assertTrue(ds.login_present() and ds.email_login())
        self.assertEqual(ds.fleet_login(), "my-fleet")
        self.assertEqual(ds.read_answer_key(), (LOGIN_ANSWER, GRANT))
        self.assertIsNone(ds.fleet_join())  # signed in: never joins again
        self.assertEqual(len([c for c in self.calls if c[1] == ds.FLEET_JOIN_PATH]), 1)
        # This folder's agent: set up on the login in the background, as /datastash:install does.
        ds.fleet_setup_kick({"session_id": "s1", "cwd": self.folder, "hook_event_name": "SessionStart"})
        bg = [a for a, _cwd in self.spawned if "fleet-setup" in a]
        self.assertEqual(len(bg), 1)
        name = bg[0][bg[0].index("--name") + 1]
        self.assertEqual(ds.cmd_fleet_setup(["--name", name, "--folder", self.folder]), 0)
        self.assertEqual([p for p in self.paths() if p != ds.FLEET_JOIN_PATH][:3],
                         [ds.AGENT_REGISTER_PATH, ds.AGENT_KEY_PATH, ds.PICKUP_PATH])
        self.assertEqual(ds.read_key("web-app"), KEY)
        # The listener never wakes the model to set it up on a fleet login.
        self.assertIsNone(ds.auto_setup_wake({"session_id": "s9", "cwd": self.folder}, self.folder, None))

    def test_the_env_key_joins_and_a_refused_key_is_said_once(self):
        os.environ["DATASTASH_FLEET_KEY"] = FLEET_KEY
        self.answers[ds.FLEET_JOIN_PATH] = (401, {"error": "fleet_key_refused"})
        self.assertEqual(ds.fleet_join(), ds.FLEET_REFUSED_LINE)
        self.assertIsNone(ds.fleet_join())
        self.assertEqual(len([c for c in self.calls if c[1] == ds.FLEET_JOIN_PATH]), 1)
        self.assertFalse(ds.login_present())

    def test_a_key_file_others_can_read_is_not_used(self):
        ds.ensure_dirs()
        fd = os.open(ds.FLEET_KEY_FILE, os.O_WRONLY | os.O_CREAT, 0o644)
        os.write(fd, FLEET_KEY.encode())
        os.close(fd)
        os.chmod(ds.FLEET_KEY_FILE, 0o644)
        self.assertEqual(ds.fleet_join(), ds.FLEET_FILE_OPEN_LINE)
        self.assertEqual(self.calls, [])

    def test_no_key_no_join(self):
        self.assertIsNone(ds.fleet_join())
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
