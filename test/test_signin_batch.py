#!/usr/bin/env python3
"""Offline tests for XLI-419 sign-in batch (plugin 1.0.40).
- `datastash computers` and `datastash computer remove NAME`: the person's own typed lines, checked by the checker;
  nothing goes out until then. The remove is signed with this computer's login answer key, with the login's own grant
  id as the agent id and the action `computer_remove:<lowercased name>`. A line not typed by the person sends nothing.
- The install's "name is taken" line shows the server's own line when it names `datastash computer remove`.
- The signed-out line: a refresh answered 401 with `signed_out` is kept; the next session start says it once, with why,
  when and the way back; a new sign-in clears it. A signed-in computer marks the Setup rows of a signed-out
  computer's agents "signed out" with when and why.
- Exposure #2: on Linux, a login key is kept encrypted by systemd-creds when it works (checked by decrypting it again);
  otherwise the plain mode-600 file, as before. Both forms are read; any fault keeps the plain file.
No network; a throwaway HOME and config folder, enforced by test/harness.py. A fake systemd-creds stands in for the tool.
Run: python3 -I test/test_signin_batch.py"""
import hashlib
import hmac
import json
import os
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_fleet_key import ACCESS, GRANT, LOGIN_ANSWER, REFRESH, FleetKey  # noqa: E402
from test_install import ds  # noqa: E402

COMPUTERS = [
    {"id": "aaaabbbb", "name": "this-mac", "os": "macOS", "last_seen": "2026-11-28T10:00:00Z", "agents": ["web-app"],
     "how": "email code", "state": "signed in", "signed_out_at": None, "why": None, "this_computer": True},
    {"id": "ccccdddd", "name": "old-laptop", "os": "Linux", "last_seen": "2026-11-16T10:00:00Z", "agents": ["notes"],
     "how": "email code", "state": "signed out", "signed_out_at": "2026-11-20T08:00:00Z", "why": "its sign-in was copied",
     "this_computer": False},
]


class Computers(FleetKey):
    def setUp(self):
        super().setUp()
        self.answers.update({
            ds.COMPUTERS_PATH: (200, {"computers": COMPUTERS}),
            ds.COMPUTER_REMOVE_PATH: (200, {"removed": True, "name": "old-laptop", "freed_agents": ["notes"]}),
        })

    def grant_mac_ok(self, proof, action, pid, typed):
        lines = [ds.TYPED_SYNC_LABEL, f"grant:{GRANT}", GRANT, action, pid, hashlib.sha256(typed.encode()).hexdigest(),
                 str(proof["typed_at"]), str(proof["iat"])]
        want = hmac.new(LOGIN_ANSWER.encode(), "\n".join(lines).encode(), hashlib.sha256).hexdigest()
        return proof["agent_id"] == GRANT and hmac.compare_digest(want, proof["mac"])

    def test_parse_the_typed_lines(self):
        self.assertEqual(ds.computers_prompt_of("datastash computers"), ("list",))
        self.assertEqual(ds.computers_prompt_of("datastash computer list"), ("list",))
        self.assertEqual(ds.computers_prompt_of("datastash computer remove old-laptop"), ("remove", "old-laptop"))
        self.assertEqual(ds.computers_prompt_of("datastash computer remove ccccdddd"), ("remove", "ccccdddd"))
        for bad in ("datastash computer remove", "datastash computer remove old laptop", "datastash computers all",
                    "datastash computer remove -rf"):
            self.assertEqual(ds.computers_prompt_of(bad), "usage", bad)
        self.assertIsNone(ds.computers_prompt_of("which computers do I have?"))

    def test_list_waits_for_the_typed_line_then_shows_each_computer(self):
        self.signed_in_person()
        _v, pid = self.prompt("datastash computers")
        self.assertEqual(self.calls, [])
        self.check(pid)
        msg = self.stop()["systemMessage"]
        self.assertIn("this-mac", msg)
        self.assertIn("(this computer)", msg)
        self.assertIn("old-laptop", msg)
        self.assertIn("signed out 2026-11-20 (its sign-in was copied)", msg)
        self.assertIn("agents: notes", msg)
        self.assertIn("datastash computer remove NAME", msg)
        self.assertNotIn("/mcp", msg)

    def test_remove_is_signed_with_the_login_and_frees_the_names(self):
        self.signed_in_person()
        text = "datastash computer remove Old-Laptop"
        _v, pid = self.prompt(text)
        self.assertEqual(self.calls, [])
        self.check(pid)
        rm = [c for c in self.calls if c[1] == ds.COMPUTER_REMOVE_PATH]
        self.assertEqual(len(rm), 1)
        _m, _p, tok, body = rm[0]
        self.assertEqual(tok, ACCESS)
        self.assertEqual(body["name"], "Old-Laptop")
        self.assertTrue(self.grant_mac_ok(body["typed_proof"], "computer_remove:old-laptop", pid, text))
        msg = self.stop()["systemMessage"]
        self.assertRegex(msg, r"old-laptop\W* is removed")
        self.assertIn("notes is free again", msg)

    def test_remove_not_typed_by_the_person_sends_nothing(self):
        self.signed_in_person()
        _v, pid = self.prompt("datastash computer remove old-laptop", source="sdk")
        self.check(pid)
        self.assertNotIn(ds.COMPUTER_REMOVE_PATH, self.paths())

    def test_the_servers_refusal_lines(self):
        self.signed_in_person()
        self.answers[ds.COMPUTER_REMOVE_PATH] = (400, {"error": "this_computer",
                                                       "message": "That is this computer. To sign it out, type datastash logout here."})
        _v, pid = self.prompt("datastash computer remove this-mac")
        self.check(pid)
        self.assertIn("**datastash logout**", self.stop()["systemMessage"])

    def test_not_signed_in_or_a_fleet_machine_is_refused_at_once(self):
        v, _pid = self.prompt("datastash computers")
        self.assertEqual(v.get("decision"), "block")
        self.assertIn("not signed in", v["reason"])
        self.assertEqual(self.calls, [])

    def test_the_name_taken_line_names_the_computer(self):
        self.signed_in_person()
        line = ("web-app is on old-laptop (last seen 12 days ago). Type **datastash computer remove old-laptop** to free "
                "it. Or pick another name. Nothing was saved.")
        self.answers[ds.AGENT_REGISTER_PATH] = (409, {"code": "agent_name_taken", "message": line})
        got = ds.login_install("web-app", self.folder)
        self.assertIn("is on old-laptop", got)
        self.assertIn("**datastash computer remove old-laptop**", got)
        self.answers[ds.AGENT_REGISTER_PATH] = (409, {"code": "agent_name_taken", "message": "Pick another name."})
        self.assertEqual(ds.login_install("web-app", self.folder), ds.NAME_TAKEN_LINE)

    # --- the signed-out line
    def test_a_signed_out_refresh_is_kept_and_said_once_at_the_next_start(self):
        ds._save_login(REFRESH, ACCESS, 900, LOGIN_ANSWER, GRANT)
        ds._drop(ds.LOGIN_ACCESS_FILE)
        self.answers[ds.DEVICE_REFRESH_PATH] = (401, {"error": "invalid_grant",
                                                      "signed_out": {"at": "2026-11-27T09:00:00Z", "why": "computer_remove"}})
        self.assertIsNone(ds.login_token(force=True))
        self.assertFalse(ds.login_present())
        inp = {"session_id": "s7", "cwd": self.folder, "hook_event_name": "SessionStart"}
        line = ds.signed_out_self_line(inp)
        self.assertEqual(line, "This computer is signed out (removed, 2026-11-27). To bring it back, type "
                               "**datastash sync you@email.com**.")
        self.assertIsNone(ds.signed_out_self_line(inp))  # once per session
        self.assertIsNotNone(ds.signed_out_self_line(dict(inp, session_id="s8")))
        ds._save_login(REFRESH, ACCESS, 900, LOGIN_ANSWER, GRANT)  # signed in again: never said again
        self.assertIsNone(ds.signed_out_self_line(dict(inp, session_id="s9")))
        self.assertFalse(os.path.exists(ds.SIGNED_OUT_FILE))

    def test_a_plain_401_keeps_no_signed_out_fact(self):
        ds._save_login(REFRESH, ACCESS, 900, LOGIN_ANSWER, GRANT)
        ds._drop(ds.LOGIN_ACCESS_FILE)
        self.answers[ds.DEVICE_REFRESH_PATH] = (401, {"error": "invalid_grant"})
        self.assertIsNone(ds.login_token(force=True))
        self.assertIsNone(ds.signed_out_self_line({"session_id": "s7", "cwd": self.folder}))

    def test_setup_rows_of_a_signed_out_computers_agents(self):
        self.signed_in_person()
        rows = {"notes": {"agent": "notes", "registered": "yes", "next step": ""},
                "web-app": {"agent": "web-app", "registered": "yes"}}
        got = ds.signed_out_setup_rows(rows)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["agent"], "notes")
        self.assertEqual(got[0]["registered"], "signed out 2026-11-20 (its sign-in was copied)")
        self.assertIn("datastash computer remove old-laptop", got[0]["next step"])
        self.assertEqual(ds.signed_out_setup_rows({}), [])
        self.assertEqual(ds.signed_out_setup_rows({"web-app": rows["web-app"]}), [])  # no row of its own made


FAKE_CREDS = """#!/bin/sh
# A stand-in for systemd-creds: "encrypt" reverses and tags the input; "decrypt" undoes it. FAKE_CREDS_MODE picks a fault.
verb="$1"; shift
case "$FAKE_CREDS_MODE" in fail) exit 1;; esac
if [ "$1" = "--user" ] && [ "$FAKE_CREDS_MODE" = "nouser" ]; then echo "unrecognized option '--user'" >&2; exit 1; fi
if [ "$verb" = encrypt ]; then printf 'FAKE:'; rev; exit 0; fi
if [ "$verb" = decrypt ]; then
  [ "$FAKE_CREDS_MODE" = "baddecrypt" ] && exit 1
  sed 's/^FAKE://' | rev; exit 0
fi
exit 2
"""


class Creds(FleetKey):
    """XLI-419 exposure #2."""
    def setUp(self):
        super().setUp()
        self.bin = tempfile.mkdtemp(prefix="creds-bin-")
        tool = os.path.join(self.bin, "systemd-creds")
        with open(tool, "w") as f:
            f.write(FAKE_CREDS)
        os.chmod(tool, 0o755)
        self.saved_path, self.saved_platform = os.environ.get("PATH", ""), ds.sys.platform
        os.environ["PATH"] = self.bin + os.pathsep + self.saved_path
        os.environ.pop("DATASTASH_NO_CREDS", None)
        os.environ.pop("FAKE_CREDS_MODE", None)
        ds._CREDS_SEEN.clear()
        ds.sys.platform = "linux"

    def tearDown(self):
        os.environ["PATH"] = self.saved_path
        os.environ.pop("FAKE_CREDS_MODE", None)
        ds.sys.platform = self.saved_platform
        ds._CREDS_SEEN.clear()
        super().tearDown()

    def raw(self, path):
        with open(path) as f:
            return f.read().strip()

    def test_the_login_keys_are_kept_encrypted_and_read_back(self):
        ds._save_login(REFRESH, ACCESS, 900, LOGIN_ANSWER, GRANT)
        for path, secret in ((ds.LOGIN_REFRESH_FILE, REFRESH), (ds._answer_key_file(), LOGIN_ANSWER)):
            text = self.raw(path)
            self.assertTrue(text.startswith("dscred1:u:"), path)
            self.assertNotIn(secret, text)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        ds._CREDS_SEEN.clear()
        self.assertEqual(ds.refresh_token(), REFRESH)
        self.assertEqual(ds.read_answer_key(), (LOGIN_ANSWER, GRANT))
        self.assertTrue(ds.login_present())
        ds._store_refresh("dsrt_" + "n" * 40)  # a rotated token is kept encrypted too
        self.assertTrue(self.raw(ds.LOGIN_REFRESH_FILE).startswith("dscred1:"))
        self.assertEqual(ds.refresh_token(), "dsrt_" + "n" * 40)

    def test_the_host_key_is_used_when_the_user_key_is_not_there(self):
        os.environ["FAKE_CREDS_MODE"] = "nouser"
        ds._save_login(REFRESH, ACCESS, 900, LOGIN_ANSWER, GRANT)
        self.assertTrue(self.raw(ds.LOGIN_REFRESH_FILE).startswith("dscred1:h:"))
        ds._CREDS_SEEN.clear()
        self.assertEqual(ds.refresh_token(), REFRESH)

    def test_any_fault_keeps_the_plain_file(self):
        for mode in ("fail", "baddecrypt"):
            with self.subTest(mode=mode):
                os.environ["FAKE_CREDS_MODE"] = mode
                ds._CREDS_SEEN.clear()
                ds._save_login(REFRESH, ACCESS, 900, LOGIN_ANSWER, GRANT)
                self.assertEqual(self.raw(ds.LOGIN_REFRESH_FILE), REFRESH)
                self.assertEqual(ds.refresh_token(), REFRESH)
        os.environ.pop("FAKE_CREDS_MODE")
        os.environ["DATASTASH_NO_CREDS"] = "1"
        ds._save_login(REFRESH, ACCESS, 900, LOGIN_ANSWER, GRANT)
        self.assertEqual(self.raw(ds.LOGIN_REFRESH_FILE), REFRESH)
        os.environ.pop("DATASTASH_NO_CREDS")
        ds.sys.platform = "darwin"
        self.assertIsNone(ds._creds_encrypt(REFRESH))

    def test_a_plain_file_from_an_earlier_version_still_reads(self):
        ds.ensure_dirs()
        ds.write_private(ds.LOGIN_REFRESH_FILE, REFRESH + "\n")
        ds.write_private(ds.LOGIN_FILE, json.dumps({"store": "file", "at": 1, "login_id": "x" * 22}))
        self.assertEqual(ds.refresh_token(), REFRESH)
        self.assertTrue(ds.login_present())

    def test_an_encrypted_file_that_no_longer_decrypts_reads_as_no_key(self):
        ds._save_login(REFRESH, ACCESS, 900, LOGIN_ANSWER, GRANT)
        ds._CREDS_SEEN.clear()
        os.environ["FAKE_CREDS_MODE"] = "baddecrypt"
        self.assertIsNone(ds.refresh_token())  # no crash; the person signs in again


class FolderNames(FleetKey):
    """XLI-419 (the quick check's bad_agent_name): a folder whose name has "datastash" or "person" as a word gets a
    name the server takes."""
    def test_reserved_words_are_dropped_from_a_folder_name(self):
        self.assertEqual(ds.without_reserved_words("datastash-claude-code"), "claude-code")
        self.assertEqual(ds.without_reserved_words("person_finder"), "finder")
        self.assertEqual(ds.without_reserved_words("personal-bot"), "personal-bot")  # a longer word stays
        self.assertEqual(ds.without_reserved_words("datastash"), "")
        self.assertTrue(ds.name_reserved("datastash-qc") and ds.name_reserved("p.e.r.s.o.n"))
        self.assertFalse(ds.name_reserved("qc-11271433"))
        folder = os.path.join(ds.HOME, "code", "datastash-qc")
        os.makedirs(folder, exist_ok=True)
        self.assertEqual(ds.auto_name(folder, None), ("name", "qc"))


if __name__ == "__main__":
    unittest.main()
