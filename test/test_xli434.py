#!/usr/bin/env python3
"""Offline tests for XLI-434 (plugin 1.0.31): the connector after the email-code sign-in, a restart or a resume.
- Claude Code keeps a 15-minute "needs authentication" mark per connector (mcp-needs-auth-cache.json) and skips that
  connector in every new or resumed session while it lasts. Once this computer is signed in, the plugin removes its own
  connector's entry (and nothing else): at the sign-in, at each session start and at each turn's end.
- The connector's helper waits long enough for another process's token refresh at a restart (no token = the mark).
- The code sign-in's "Done." says plainly that the datastash command works now and the tools come with the next session.
- Install, uninstall and make-private/public load the connector's deferred tools with ToolSearch before deciding the
  connector is missing.
- A session that runs an older copy than Claude Code has installed says once: "Start a new session to use datastash X."
- doctor names the version this session actually loaded (its session start's record, else Claude Code's in-use marker).
No network, no Claude Code run; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_xli434.py"""
import json
import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_email_signin as es  # noqa: E402
ACCESS = es.ACCESS
from test_install import ds  # noqa: E402
import harness  # noqa: E402,F401

CACHE = os.path.join(ds.CLAUDE_DIR, ds.NEEDS_AUTH_CACHE)
OTHER = {"timestamp": 1795836198059}


class Patch(unittest.TestCase):
    def patch(self, name, value):
        old = getattr(ds, name)
        setattr(ds, name, value)
        self.addCleanup(setattr, ds, name, old)

    def write_cache(self, data):
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w") as f:
            json.dump(data, f)
        self.addCleanup(lambda: os.path.exists(CACHE) and os.unlink(CACHE))

    def read_cache(self):
        with open(CACHE) as f:
            return json.load(f)


class NeedsAuthMark(Patch):
    def test_signed_in_removes_only_this_connectors_entry(self):
        self.patch("login_present", lambda: True)
        self.write_cache({ds.CONNECTOR_SERVER_KEY: dict(OTHER), "plugin:other:other": dict(OTHER)})
        self.assertTrue(ds.clear_connector_auth_mark())
        self.assertEqual(self.read_cache(), {"plugin:other:other": OTHER})
        self.assertFalse(ds.clear_connector_auth_mark())  # nothing left to remove
        self.assertEqual([n for n in os.listdir(os.path.dirname(CACHE)) if ".datastash-" in n], [])  # no temp file left

    def test_not_signed_in_keeps_the_mark(self):
        self.patch("login_present", lambda: False)
        self.write_cache({ds.CONNECTOR_SERVER_KEY: dict(OTHER)})
        self.assertFalse(ds.clear_connector_auth_mark())
        self.assertIn(ds.CONNECTOR_SERVER_KEY, self.read_cache())

    def test_no_file_or_a_bad_file_is_left_alone(self):
        self.patch("login_present", lambda: True)
        self.assertFalse(ds.clear_connector_auth_mark())
        self.assertFalse(os.path.exists(CACHE))
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w") as f:
            f.write("not json")
        self.addCleanup(os.unlink, CACHE)
        self.assertFalse(ds.clear_connector_auth_mark())
        with open(CACHE) as f:
            self.assertEqual(f.read(), "not json")

    def test_a_symlink_is_never_followed(self):
        self.patch("login_present", lambda: True)
        target = os.path.join(os.path.dirname(CACHE), "elsewhere.json")
        with open(target, "w") as f:
            json.dump({ds.CONNECTOR_SERVER_KEY: OTHER}, f)
        self.addCleanup(os.unlink, target)
        os.symlink(target, CACHE)
        self.addCleanup(os.unlink, CACHE)
        self.assertFalse(ds.clear_connector_auth_mark())
        with open(target) as f:
            self.assertIn(ds.CONNECTOR_SERVER_KEY, json.load(f))

    def test_the_turns_end_clears_it(self):
        self.patch("login_present", lambda: True)
        self.patch("_status_check", lambda: 0)
        self.write_cache({ds.CONNECTOR_SERVER_KEY: dict(OTHER)})
        self.assertEqual(ds.cmd_status_check([]), 0)
        self.assertEqual(self.read_cache(), {})


class SignInClearsTheMark(es.EmailSignIn):
    def test_code_sign_in_clears_the_mark_and_says_the_next_session_line(self):
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w") as f:
            json.dump({ds.CONNECTOR_SERVER_KEY: OTHER}, f)
        self.addCleanup(lambda: os.path.exists(CACHE) and os.unlink(CACHE))
        self.signed_in_by_code()
        with open(CACHE) as f:
            self.assertEqual(json.load(f), {})
        line = self.stop()["systemMessage"]
        self.assertEqual(line, "datastash: Done. " + ds.NEXT_SESSION_LINE)
        self.assertNotIn("/mcp", line)
        self.assertNotIn("Authenticate", line)


class HelperWaitsForTheRefresh(Patch):
    def setUp(self):
        for k in ("LOGIN_FILE", "LOGIN_ACCESS_FILE", "LOGIN_LOCK_FILE"):
            self.patch(k, os.path.join(ds.CONF, "t434-" + os.path.basename(getattr(ds, k))))
        ds.ensure_dirs()

    def hold_lock_then_save(self, seconds):
        import fcntl
        fd = os.open(ds.LOGIN_LOCK_FILE, os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)

        def later():
            time.sleep(seconds)
            ds._save_access(ACCESS, 900)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        t = threading.Thread(target=later)
        t.start()
        return t

    def test_default_wait_gives_up_while_another_process_refreshes(self):
        t = self.hold_lock_then_save(2.6)
        self.assertIsNone(ds.login_token())
        t.join()

    def test_helper_wait_gets_the_token_another_process_saved(self):
        t = self.hold_lock_then_save(2.6)
        self.assertEqual(ds.login_token(lock_wait=ds.HELPER_LOCK_WAIT), ACCESS)
        t.join()
        self.assertLess(ds.HELPER_LOCK_WAIT + 1, 10)  # inside Claude Code's 10 s headersHelper limit

    def test_the_helper_uses_the_longer_wait(self):
        got = []
        self.patch("login_token", lambda force=False, lock_wait=None: got.append(lock_wait) or ACCESS)
        self.patch("private_helper_header", lambda: None)
        self.patch("connector_helper_call", lambda: True)
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ds.cmd_token(["--header"]), 0)
        self.assertEqual(got, [ds.HELPER_LOCK_WAIT])
        self.assertEqual(json.loads(out.getvalue()), {"Authorization": "Bearer " + ACCESS})


class DeferredTools(unittest.TestCase):
    def test_install_note_loads_exactly_its_tools_first(self):
        note = ds.INSTALL_AI.format(steps="", load=ds.load_tools_ai("ledger_agents_list", "ledger_agents_note"))
        self.assertIn('ToolSearch, query "select:mcp__plugin_datastash_datastash__ledger_agents_list,'
                      'mcp__plugin_datastash_datastash__ledger_agents_note"', note)
        self.assertLess(note.index("ToolSearch"), note.index("If no datastash connector tool is there"))
        self.assertIn("even after ToolSearch", note)
        self.assertNotIn("/mcp", note)

    def test_uninstall_and_visibility_notes_load_the_tools_too(self):
        for note in (ds.UNINSTALL_LIST_AI, ds.VIS_AI):
            self.assertIn("select:mcp__plugin_datastash_datastash__ledger_agents_list", note)


class OlderCopy(Patch):
    def test_older_copy_than_installed_says_the_line_once_per_session(self):
        self.patch("PLUGIN_VERSION", "1.0.30")
        self.patch("installed_version", lambda root=None: "1.0.31")
        inp = {"session_id": "t434-resume-1", "source": "resume"}
        self.assertEqual(ds.new_session_line(inp, None), "Start a new session to use datastash 1.0.31.")
        self.assertIsNone(ds.new_session_line(inp, None))
        self.assertEqual(ds.new_session_line({"session_id": "t434-resume-2"}, None),
                         "Start a new session to use datastash 1.0.31.")

    def test_same_or_newer_copy_says_nothing(self):
        self.patch("PLUGIN_VERSION", "1.0.31")
        for disk in ("1.0.31", "1.0.30", None):
            self.patch("installed_version", lambda root=None, d=disk: d)
            self.assertIsNone(ds.new_session_line({"session_id": "t434-same"}, None))


class LoadedVersion(Patch):
    ME = {"sid": "t434", "proc": [424242, "77"], "chain": [[424242, "77"]]}

    def test_session_record_wins_over_the_copy_that_runs(self):
        self.patch("PLUGIN_VERSION", "1.0.26")
        ds.record_session_plugin(self.ME)
        self.addCleanup(lambda: os.unlink(ds._session_plugin_path(self.ME["proc"])))
        self.patch("PLUGIN_VERSION", "1.0.31")  # doctor run from a newer folder
        self.assertEqual(ds.session_loaded_version(self.ME), ("1.0.26", "session"))
        got = ds.version_report("1.0.31", "1.0.31", ds.session_loaded_version(self.ME)[0])
        self.assertEqual(got, [("note", "version: this session runs datastash 1.0.26; installed: 1.0.31; newest: 1.0.31. "
                                        "Start a new session to use datastash 1.0.31.")])

    def test_in_use_marker_is_the_fallback(self):
        me = {"sid": "t434b", "proc": [434343, "88"], "chain": [[434343, "88"]]}
        base = os.path.join(ds.CLAUDE_DIR, "plugins", "cache", "datastash", "datastash")
        for v in ("1.0.26", "1.0.31"):
            os.makedirs(os.path.join(base, v, ".in_use"), exist_ok=True)
        marker = os.path.join(base, "1.0.26", ".in_use", "434343")
        open(marker, "w").close()
        self.addCleanup(os.unlink, marker)
        self.assertEqual(ds.session_loaded_version(me), ("1.0.26", "marker"))

    def test_outside_a_session_nothing_is_claimed(self):
        self.assertEqual(ds.session_loaded_version({"sid": None, "proc": None, "chain": []}), (None, None))

    def test_records_of_gone_processes_are_dropped(self):
        gone = [999999999, "1"]
        ds.ensure_dirs()
        ds.write_private(ds._session_plugin_path(gone), json.dumps({"version": "1.0.1"}))
        ds.record_session_plugin(self.ME)
        self.addCleanup(lambda: os.unlink(ds._session_plugin_path(self.ME["proc"])))
        self.assertFalse(os.path.exists(ds._session_plugin_path(gone)))


if __name__ == "__main__":
    unittest.main()
