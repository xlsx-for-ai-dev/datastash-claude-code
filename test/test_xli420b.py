#!/usr/bin/env python3
"""Offline tests for XLI-420 part 2 (plugin 1.0.32): datastash in the Claude desktop app's Code tab.
- The Code tab's command forms: the picker's /install and /uninstall, the bare skill name (datastash:install) and the
  tag block naming /install, only for the datastash install and uninstall commands, only with the same arguments, and
  only where the hook proved the desktop app from the signed process table (test/fixtures/desktop-process-chains.json).
- The plain-text trigger "install datastash" (the person's typed line, terminal or Code tab).
- "datastash sync me": the email this computer already knows (its own earlier email sign-in, else the Claude login's
  account email), with the same typed proof; with none known, one plain line and nothing tried.
No network; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_xli420b.py"""
import io
import json
import os
import sys
import time
import unittest
import uuid
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_install import ALPHA, BETA, FIXTURES, LIST, OWN, Base, ds  # noqa: E402
import test_email_signin  # noqa: E402
EMAIL = test_email_signin.EMAIL
from test_status import desktop_table  # noqa: E402

ATTACKS = ("headless_p", "nested_no_print", "nested_same_binary", "setsid", "fake_app_folder", "unsigned_at_real_path",
           "renamed_binary", "renamed_as_shell", "helper_from_agent", "app_from_shell", "app_with_options", "terminal", None)


def desktop_fields():
    """The fields the real Code tab (Claude Code 2.1.332 as the desktop host) writes on a typed slash command."""
    with open(os.path.join(FIXTURES, "desktop-code-tab-2.1.332.jsonl")) as f:
        own = [json.loads(x) for x in f if x.strip()][0]
    return {k: own[k] for k in ("origin", "entrypoint", "turnOrigin", "isSidechain", "userType", "version")}


def desktop_typed_fields():
    with open(os.path.join(FIXTURES, "desktop-code-tab-2.1.332.jsonl")) as f:
        own = [json.loads(x) for x in f if x.strip()][-1]
    return {k: own[k] for k in ("origin", "entrypoint", "turnOrigin", "isSidechain", "userType", "version", "promptSource")}


def tag(message, name, args=""):
    return (f"<command-message>{message}</command-message>\n<command-name>/{name}</command-name>"
            + (f"\n<command-args>{args}</command-args>" if args else ""))


class CodeTabForms(Base):
    CONNECTOR_SIGNED_IN = True

    def setUp(self):
        super().setUp()
        self.installed()

    def typed_as(self, hook_prompt, content, fields, sid="s1"):
        """The person's prompt: the hook gets hook_prompt; Claude Code then writes content with these fields."""
        pid = str(uuid.uuid4())
        v = self.hook(ds.cmd_prompt, {"session_id": sid, "prompt": hook_prompt, "hook_event_name": "UserPromptSubmit",
                                      "cwd": self.folder, "prompt_id": pid, "transcript_path": self.transcript(sid)})
        e = dict(fields, type="user", promptId=pid, sessionId=sid, message={"role": "user", "content": content},
                 timestamp=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()))
        with open(self.transcript(sid), "a") as f:
            f.write(json.dumps(e) + "\n")
        return v

    def uninstall_note(self, hook_prompt, content, fields=None):
        self.typed_as(hook_prompt, content, desktop_fields() if fields is None else fields)
        self.tool("PostToolUse", "ledger_agents_list", {}, LIST)
        return self.tool("PreToolUse", "ledger_agents_note", {})

    def passed(self, out):
        return "updatedInput" in (out or {}).get("hookSpecificOutput", {})

    def test_code_tab_forms_pass_on_the_signed_desktop_chain(self):
        """The picker's /uninstall, the tag block naming /uninstall (with either message), the bare skill name, and
        the full name, each with the same names, pass on the recorded Code-tab chain."""
        args = "alpha beta web-app"
        for hook_prompt, content, fields in (
                (f"/uninstall {args}", tag("uninstall", "uninstall", args), None),
                (f"/uninstall {args}", tag("datastash:uninstall", "uninstall", args), None),
                (f"/uninstall {args}", tag("datastash:uninstall", "datastash:uninstall", args), None),
                (f"/datastash:uninstall {args}", tag("uninstall", "uninstall", args), None),
                (f"datastash:uninstall {args}", f"datastash:uninstall {args}", desktop_typed_fields()),
                (f"/uninstall {args}", f"/uninstall {args}", desktop_typed_fields())):
            desktop_table(self, "code_tab")
            out = self.uninstall_note(hook_prompt, content, fields)
            self.assertTrue(self.passed(out), (hook_prompt, content))
            self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["targets"], sorted([OWN, ALPHA, BETA]))

    def test_code_tab_forms_are_refused_on_every_attack_chain(self):
        for chain in ATTACKS:
            desktop_table(self, chain)
            out = self.uninstall_note("/uninstall alpha beta web-app", tag("uninstall", "uninstall", "alpha beta web-app"))
            self.assertFalse(self.passed(out), chain)

    def test_slash_install_is_no_datastash_command_outside_the_desktop_app(self):
        """In a terminal (or any unproven chain) /install, /uninstall and datastash:install are someone else's."""
        for chain in ("terminal", "headless_p", None):
            desktop_table(self, chain)
            for p in ("/install", "/install web-app", "/uninstall alpha", "datastash:install"):
                self.assertIsNone(ds.command_prompt({"session_id": "s1", "prompt": p, "hook_event_name": "UserPromptSubmit",
                                                     "cwd": self.folder, "prompt_id": str(uuid.uuid4()),
                                                     "transcript_path": self.transcript("s1")}), (chain, p))
        self.assertIsNone(ds.load_cmd("s1"))

    def test_other_names_arguments_and_shapes_are_refused(self):
        """Another command, other arguments, the tag block's two names disagreeing, a tag block not stamped as a typed
        command, a plain 'install', or an agent's text is refused on the desktop chain too."""
        args = "alpha beta web-app"
        for hook_prompt, content, fields in (
                (f"/uninstall {args}", tag("other:uninstall", "other:uninstall", args), None),
                (f"/uninstall {args}", tag("uninstall", "uninstall", "alpha"), None),
                (f"/uninstall {args}", tag("install", "uninstall", args), None),
                (f"/uninstall {args}", tag("uninstall", "uninstall", args), desktop_typed_fields()),
                (f"/uninstall {args}", f"uninstall {args}", desktop_typed_fields()),
                (f"/uninstall {args}", tag("uninstall", "uninstall", args), dict(desktop_fields(), origin=None)),
                (f"/uninstall {args}", f"/uninstall {args}", dict(desktop_typed_fields(), origin=None)),
                (f"/uninstall {args}", tag("uninstall", "uninstall", args), dict(desktop_fields(), isSidechain=True)),
                (f"/uninstall {args}", tag("uninstall", "uninstall", args), dict(desktop_fields(), entrypoint="sdk-cli"))):
            desktop_table(self, "code_tab")
            out = self.uninstall_note(hook_prompt, content, fields)
            self.assertFalse(self.passed(out), (content, fields))
        self.assertIsNone(ds.desktop_cmd_form("install"))
        self.assertIsNone(ds.desktop_cmd_form("install web-app"))
        self.assertIsNone(ds.desktop_cmd_form("/installer"))
        self.assertEqual(ds.desktop_cmd_form("/install  web-app "), ("install", "web-app"))
        self.assertEqual(ds.desktop_cmd_form(tag("datastash:install", "install")), ("install", ""))

    def test_code_tab_install_on_the_connector(self):
        """The picker's /install NAME: the install's steps go to the model as for /datastash:install."""
        desktop_table(self, "code_tab")
        v = self.typed_as("/install web-app", tag("install", "install", "web-app"), desktop_fields())
        self.assertIn("additionalContext", v["hookSpecificOutput"])
        rec = ds.load_cmd("s1")
        self.assertEqual((rec["kind"], rec["desktop"]), ("install", True))
        self.assertEqual(rec["name"], "web-app")

    def test_install_datastash_plain_text(self):
        """'install datastash' is the install command typed as plain text: terminal (typed) and Code tab (signed)."""
        m = ds.INSTALL_RE.fullmatch("install datastash")
        self.assertTrue(m and m.group(1) is None)
        self.assertEqual(ds.INSTALL_RE.fullmatch("Install datastash web-app").group(1), "web-app")
        v, _pid = self.prompt("install datastash")
        self.assertEqual(ds.load_cmd("s1")["kind"], "install")
        self.assertIn("additionalContext", v["hookSpecificOutput"])


class CodeTabEmailLogin(test_email_signin.EmailSignIn):
    """On an emailed-code login the plugin itself installs: the Code tab's /install and 'install datastash' too."""

    def test_code_tab_install_and_install_datastash_on_the_login(self):
        self.signed_in_by_code()
        self.stop()
        ds._drop(ds.key_path("web-app"))
        for hook_prompt, content, fields, chain, ok in (
                ("/install web-app", tag("install", "install", "web-app"), desktop_fields(), "code_tab", True),
                ("/install web-app", tag("install", "install", "web-app"), desktop_fields(), "headless_p", False),
                ("install datastash web-app", "install datastash web-app", desktop_typed_fields(), "code_tab", True),
                ("install datastash web-app", "install datastash web-app", desktop_typed_fields(), "setsid", False)):
            desktop_table(self, chain)
            ds._drop(ds.key_path("web-app"))  # this folder's agent not set up yet
            before = len(self.calls)
            pid = str(uuid.uuid4())
            self.hook(ds.cmd_prompt, {"session_id": "s1", "prompt": hook_prompt, "hook_event_name": "UserPromptSubmit",
                                      "cwd": self.folder, "prompt_id": pid, "transcript_path": self.transcript("s1")})
            e = dict(fields, type="user", promptId=pid, sessionId="s1", message={"role": "user", "content": content},
                     timestamp=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()))
            with open(self.transcript("s1"), "a") as f:
                f.write(json.dumps(e) + "\n")
            if not any(a[0][-1] == pid for a in self.spawned):
                self.assertFalse(ok, (hook_prompt, chain))
                continue
            self.assertEqual(ds.cmd_status_verify([pid]), 0)
            sent = [c[1] for c in self.calls[before:]]
            self.assertEqual(ds.AGENT_REGISTER_PATH in sent, ok, (hook_prompt, chain))


class SyncMe(test_email_signin.EmailSignIn):
    def setUp(self):
        super().setUp()
        self.saved_cfg = ds.CLAUDE_USER_CONFIG
        ds.CLAUDE_USER_CONFIG = os.path.join(ds.CONF, "claude.json")
        self.addCleanup(setattr, ds, "CLAUDE_USER_CONFIG", self.saved_cfg)

    def claude_login(self, email):
        os.makedirs(ds.CONF, exist_ok=True)
        with open(ds.CLAUDE_USER_CONFIG, "w") as f:
            json.dump({"oauthAccount": {"emailAddress": email, "accountUuid": "x"}}, f)

    def test_nothing_known_says_one_line_and_tries_nothing(self):
        v, _pid = self.prompt("datastash sync me")
        self.assertEqual(v["decision"], "block")
        self.assertIn("datastash sync you@email.com", v["reason"])
        self.assertIn("datastash sync me", v["reason"])
        self.assertEqual((self.calls, self.spawned), ([], []))

    def test_the_claude_login_email_is_used_with_the_typed_proof(self):
        self.claude_login("Tester@Example.com")
        v, pid = self.prompt("datastash sync me")
        ai = v["hookSpecificOutput"]["additionalContext"]
        self.assertIn("t***@example.com", ai)
        self.assertNotIn(EMAIL, ai)
        self.assertEqual(self.calls, [])  # nothing goes out before the transcript shows the person typed it
        self.check(pid)
        self.assertEqual(self.paths(), [ds.EMAIL_WAIT_PATH, ds.EMAIL_START_PATH])
        self.assertEqual(self.calls[1][3]["email"], EMAIL)

    def test_the_earlier_sign_in_email_wins_and_is_kept_after_the_code(self):
        self.signed_in_by_code()
        self.assertEqual(ds.read_json(ds.EMAIL_LOGIN_FILE, {}).get("email"), EMAIL)
        self.assertEqual(oct(os.stat(ds.EMAIL_LOGIN_FILE).st_mode & 0o777), "0o600")
        self.claude_login("other@example.org")
        self.assertEqual(ds.known_email(), EMAIL)
        self.stop()
        before = len(self.calls)
        _v, pid = self.prompt("Datastash sync me")
        self.check(pid)
        self.assertEqual(self.calls[before + 1][3]["email"], EMAIL)

    def test_not_typed_by_the_person_sends_nothing(self):
        self.claude_login(EMAIL)
        for source in ("sdk", "system"):
            _v, pid = self.prompt("datastash sync me", source=source)
            self.check(pid)
        self.assertEqual(self.calls, [])

    def test_code_tab_line_passes_only_on_the_signed_chain(self):
        self.claude_login(EMAIL)
        for chain, ok in (("code_tab", True), ("headless_p", False), ("fake_app_folder", False), ("setsid", False)):
            desktop_table(self, chain)
            before = len(self.calls)
            pid = str(uuid.uuid4())
            self.hook(ds.cmd_prompt, {"session_id": "s1", "prompt": "datastash sync me", "hook_event_name": "UserPromptSubmit",
                                      "cwd": self.folder, "prompt_id": pid, "transcript_path": self.transcript("s1")})
            e = dict(desktop_typed_fields(), type="user", promptId=pid, sessionId="s1",
                     message={"role": "user", "content": "datastash sync me"},
                     timestamp=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()))
            with open(self.transcript("s1"), "a") as f:
                f.write(json.dumps(e) + "\n")
            self.check(pid)
            self.assertEqual(ds.EMAIL_START_PATH in [c[1] for c in self.calls[before:]], ok, chain)

    def test_unit_forms(self):
        for p in ("datastash sync me", "sync datastash me", "  DATASTASH SYNC ME. "):
            self.assertTrue(ds.SYNC_ME_RE.fullmatch(p), p)
        for p in ("datastash sync meme", "datastash sync me please", "datastash sync"):
            self.assertFalse(ds.SYNC_ME_RE.fullmatch(p), p)
        self.assertEqual(ds.masked_email("you@email.com"), "y***@email.com")
        os.makedirs(ds.CONF, exist_ok=True)
        with open(ds.CLAUDE_USER_CONFIG, "w") as f:
            f.write("{not json")
        self.assertIsNone(ds.known_email())


class Doctor(Base):
    def test_doctor_names_the_desktop_proof(self):
        for chain, want in (("code_tab", "note: desktop proof: pass"),
                            ("headless_p", "note: desktop proof: not proven (step: another process between)")):
            desktop_table(self, chain)
            with redirect_stdout(io.StringIO()) as out:
                ds.cmd_doctor([])
            self.assertIn(want, out.getvalue().splitlines(), chain)
            self.assertNotIn(ds.HOME + "/Library", out.getvalue())


if __name__ == "__main__":
    unittest.main()
