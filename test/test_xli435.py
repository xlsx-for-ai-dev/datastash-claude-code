#!/usr/bin/env python3
"""Offline tests for XLI-435 (plugin 1.0.36): CLI hygiene from the feature review.
- #6 help never writes: every command answers -h / --help (anywhere before `--`) with its usage, exit 0, and writes,
  sends and starts nothing; words after `--` are data (`remember -- --help` saves the fact "--help").
- #7 `datastash --help` lists every real command, each with a line.
- #5 status, doctor and the self-check share one listener check, which also knows an older plugin copy's listener.
- #4 the PATH line keeps one datastash (old version folders taken off, this one first, other entries kept, sh / bash /
  zsh alike); unused old version folders go to the datastash trash (newest two, this copy and folders in use kept).
- #8 release notes: a CHANGELOG line for each release 1.0.26 to 1.0.36, README links it, and the what's-new table has
  every release from 1.0.21 on.
- #15 the sign-in guard reads a file tool's paths, not the text it writes; the real guard stays.
- 1.0.30 / 1.0.31 review: the hook's "Check your email" is not repeated by the model; no /mcp in the token help or
  README Troubleshooting; the busy mark keeps its first time; the needs-auth cache is read before the sign-in check;
  no keychain read without time left for the request; new-session marks are cleaned up.
No network, no key; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_xli435.py"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness  # noqa: E402
from test_status import TMP, Base, ds  # noqa: E402  (the same harness, plugin copy and fixtures)
from test_off import OffBase  # noqa: E402

ROOT = os.path.join(HERE, "..")
PLUGIN = os.path.join(ROOT, "plugins", "datastash")


def snapshot():
    """Every file under the throwaway HOME and datastash folder, with its size and mtime."""
    out = {}
    for top in (harness.FAKE_HOME, ds.CONF):
        for d, _dirs, names in os.walk(top):
            for n in names:
                p = os.path.join(d, n)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                out[p] = (st.st_size, st.st_mtime_ns)
    return out


class HelpNeverWrites(Base):
    def setUp(self):
        super().setUp()
        self.argv = sys.argv
        self.addCleanup(setattr, sys, "argv", self.argv)
        self.addCleanup(setattr, ds, "signin_state", ds.signin_state)
        ds.signin_state = lambda **kw: self.fail("help must not start the sign-in decision")

    def main(self, *argv):
        sys.argv = ["datastash", *argv]
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            try:
                rc = ds.main()
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_every_command_answers_help_and_writes_sends_and_starts_nothing(self):
        cmds = sorted(ds.command_table())
        self.assertGreater(len(cmds), 50)
        for cmd in cmds:
            forms = (["--help"], ["-h"]) if cmd in ("timer", "timers") else (["--help"], ["-h"], ["x", "--help"],
                                                                             ["-h", "--", "x"])
            for form in forms:
                with self.subTest(cmd=cmd, form=form):
                    before = snapshot()
                    rc, out, err = self.main(cmd, *form)
                    self.assertEqual(rc, 0, err)
                    self.assertTrue(out.strip(), "help printed nothing")
                    self.assertIn(cmd if cmd not in ds.HELP_SAME else ds.HELP_SAME[cmd], out)
                    self.assertEqual(snapshot(), before, "help wrote a file")
                    self.assertEqual(self.calls, [], "help sent a call")
                    self.assertEqual(self.spawned, [], "help started a process")

    def test_remember_help_saves_nothing_and_off_help_takes_no_agent_name(self):
        rc, out, _err = self.main("remember", "--help")
        self.assertEqual((rc, self.calls), (0, []))
        self.assertIn("usage: datastash remember", out)
        rc, out, _err = self.main("off", "--help")
        self.assertEqual((rc, self.calls), (0, []))
        self.assertIn("off NAME", out)
        self.assertFalse(os.path.exists(ds.OFF) and os.listdir(ds.OFF))

    def test_words_after_the_double_dash_are_data(self):
        self.assertFalse(ds.asks_help(["--", "--help"]))
        self.assertFalse(ds.asks_help(["a", "--", "-h"]))
        self.assertTrue(ds.asks_help(["a", "-h", "--", "b"]))
        self.assertFalse(ds.asks_help(["--helper", "-hx", "help"]))
        _agent = {"name": "web-app", "slug": "web-app", "workbook_id": "wb"}
        self.addCleanup(setattr, ds, "_agent_here", ds._agent_here)
        ds._agent_here = lambda args: (dict(_agent), self.folder)
        rc, out, err = self.main("remember", "--", "--help")
        self.assertEqual(rc, 0, err)
        posts = [c for c in self.calls if c[0] == "POST" and c[1] == ds.ROWS_PATH]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][3]["row"]["fact"], "--help")
        self.assertIn("remembered", out)

    def test_help_command_and_full_help(self):
        rc, out, _err = self.main("help", "status")
        self.assertEqual(rc, 0)
        self.assertIn("datastash status --all", out)
        rc, out, _err = self.main("--help")
        self.assertEqual(rc, 0)
        for cmd in ("remember", "skills-sync", "self-check", "setup-row", "self-update", "outbox-send", "status-verify",
                    "dds-update", "timer", "estimate", "repeat", "share", "allow"):
            with self.subTest(cmd=cmd):
                self.assertRegex(out, r"(?m)^  " + re.escape(cmd) + r"(\[s\])?\b.*\S{3,}", f"{cmd} has no help line")
        listed = set()
        for line in out.splitlines():
            m = re.match(r"  (\S+)", line)
            if m and not line.startswith("   "):
                listed.add(re.sub(r"\[.*?\]", "", m.group(1)))
        hooks = set(ds.HOOK_COMMANDS)
        missing = {c for c in ds.command_table() if c not in listed and c not in hooks and c not in ds.HELP_SAME}
        self.assertEqual(missing, set(), "commands with no line in datastash --help")
        for c in hooks:
            self.assertIn(c, out)

    def test_token_help_has_no_mcp(self):
        rc, out, _err = self.main("token", "--help")
        self.assertEqual(rc, 0)
        self.assertIn("token --header", out)
        self.assertNotIn("/mcp", out)
        self.assertNotIn("/mcp", ds.__doc__)


FAKE_LISTENER = """import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
time.sleep(30)
"""


class OneListenerAnswer(Base):
    def old_copy(self, version="1.0.26"):
        root = os.path.join(harness.tmpdir(self, dir=TMP), "cache", "datastash", "datastash", version)
        os.makedirs(os.path.join(root, ".claude-plugin"))
        os.makedirs(os.path.join(root, "scripts"))
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), "w") as f:
            json.dump({"name": "datastash", "version": version}, f)
        script = os.path.join(root, "scripts", "datastash")
        with open(script, "w") as f:
            f.write(FAKE_LISTENER)
        return script

    def start(self, script, s="web-app"):
        ds.write_private(ds.lock_path(s), json.dumps({"pid": 0, "session_id": "sess1", "since": int(time.time())}))
        p = subprocess.Popen([sys.executable, "-I", script, ds.lock_path(s), "listen"], stdout=subprocess.PIPE,
                             text=True, cwd=TMP)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        self.assertEqual(p.stdout.readline().strip(), "held")
        with open(ds.lock_path(s), "r+") as f:  # the same file (the lock stays on it): only its record changes
            f.write(json.dumps({"pid": p.pid, "session_id": "sess1", "since": int(time.time())}).ljust(200))
        return p

    def answers(self):
        _rc, out, _err = self.run_cmd(ds.cmd_status, [])
        status = "listener: running" in out
        line = ds._listener_line(self.folder, {"sid": "sess1", "proc": None})
        doctor = "listener: running" in line
        check = ds._check_item("listener", {"slug": "web-app", "name": "web-app"}, self.folder)
        return status, doctor, check

    def test_status_doctor_and_self_check_agree_on_an_older_copys_listener(self):
        p = self.start(self.old_copy())
        self.assertEqual(self.answers(), (True, True, True))
        p.kill()
        p.wait()
        self.assertEqual(self.answers(), (False, False, False))

    def test_a_lock_record_with_no_listener_is_not_running_anywhere(self):
        ds.write_private(ds.lock_path("web-app"), json.dumps({"pid": os.getpid(), "session_id": "sess1"}))
        self.assertEqual(self.answers(), (False, False, False))

    def test_a_process_that_only_names_datastash_is_no_listener(self):
        script = os.path.join(harness.tmpdir(self, dir=TMP), "datastash")
        with open(script, "w") as f:
            f.write(FAKE_LISTENER)
        self.start(script)
        self.assertEqual(self.answers(), (False, False, False))


class CleanPath(Base):
    def env_line(self, path):
        env_file = os.path.join(harness.tmpdir(self, dir=TMP), "env")
        old = (os.environ.get("CLAUDE_ENV_FILE"), os.environ.get("PATH"))
        os.environ["CLAUDE_ENV_FILE"], os.environ["PATH"] = env_file, path
        try:
            ds.put_on_path()
        finally:
            for k, v in zip(("CLAUDE_ENV_FILE", "PATH"), old):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return env_file

    def test_old_versions_leave_path_and_this_copy_goes_first_once(self):
        here = os.path.join(os.path.dirname(os.path.realpath(ds.__file__)), "cli")
        cache = "/h/.claude/plugins/cache/datastash/datastash"
        path = ":".join([f"{cache}/1.0.26/scripts/cli", "/usr/bin", f"{cache}/1.0.18/scripts/cli/", "/bin",
                         f"{cache}/0.1.0/scripts", here, "/opt/my tools", f"{cache}/1.0.26/scripts/cli",
                         "/home/me/datastash/scripts"])
        env_file = self.env_line(path)
        for sh in ("sh", "bash", "zsh"):
            exe = shutil.which(sh)
            if not exe:
                continue
            with self.subTest(shell=sh):
                got = subprocess.run([exe, "-c", '. "$1"; printf %s "$PATH"', sh, env_file], env=dict(os.environ, PATH=path),
                                     capture_output=True, text=True, cwd=TMP, timeout=20).stdout
                self.assertEqual(got.split(":"), [here, "/usr/bin", "/bin", "/opt/my tools",
                                                  "/home/me/datastash/scripts"])

    def test_nothing_written_when_path_is_already_clean(self):
        here = os.path.join(os.path.dirname(os.path.realpath(ds.__file__)), "cli")
        env_file = self.env_line(f"{here}:/usr/bin:/bin")
        self.assertFalse(os.path.exists(env_file))
        env_file = self.env_line(f"{here}:/x/.claude/plugins/cache/datastash/datastash/1.0.20/scripts/cli:/bin")
        self.assertTrue(os.path.exists(env_file))

    def test_only_the_plugins_own_cache_folder_leaves_path(self):
        here = os.path.join(os.path.dirname(os.path.realpath(ds.__file__)), "cli")
        others = ["/home/me/src/datastash/2.0/scripts", "/home/me/datastash/1.0.20/scripts/cli",
                  "/x/cache/other/datastash/1.0.20/scripts"]
        self.assertFalse(any(ds._old_cli_entry(e) for e in others))
        self.assertFalse(os.path.exists(self.env_line(":".join([here] + others + ["/bin"]))))
        old = "/h/.claude/plugins/cache/datastash/datastash/1.0.20/scripts"
        path = ":".join([here, others[0], old, "/bin"])
        env_file = self.env_line(path)
        got = subprocess.run([shutil.which("sh"), "-c", '. "$1"; printf %s "$PATH"', "sh", env_file],
                             env=dict(os.environ, PATH=path), capture_output=True, text=True, cwd=TMP, timeout=20).stdout
        self.assertEqual(got.split(":"), [here, others[0], "/bin"])


def installed_plugins(plugins, rows):
    with open(os.path.join(plugins, "installed_plugins.json"), "w") as f:
        json.dump({"version": 2, "plugins": {"datastash@datastash": rows}}, f)


class OldFolders(Base):
    def cache(self, versions):
        plugins = harness.tmpdir(self, dir=TMP)
        base = os.path.join(plugins, "cache", "datastash", "datastash")
        for v in versions:
            os.makedirs(os.path.join(base, v, "scripts"))
        self.addCleanup(setattr, ds, "VERSIONS_TRASH", ds.VERSIONS_TRASH)
        ds.VERSIONS_TRASH = os.path.join(harness.tmpdir(self, dir=TMP), "trash", "plugin-versions")
        return plugins, base

    def test_a_version_installed_for_a_project_with_no_live_session_is_kept(self):
        plugins, base = self.cache(("1.0.20", "1.0.26", "1.0.30", "1.0.35", "1.0.36"))
        installed_plugins(plugins, [
            {"scope": "user", "version": "1.0.36", "installPath": os.path.join(base, "1.0.36")},
            {"scope": "project", "projectPath": "/home/me/proj-a", "version": "1.0.26",
             "installPath": os.path.join(base, "1.0.26")},
            {"scope": "local", "projectPath": "/srv/other", "version": "1.0.20",
             "installPath": os.path.join(base, "1.0.20")}])
        self.assertEqual(ds.tidy_old_versions(base), ["1.0.30"])
        self.assertEqual(sorted(os.listdir(base)), ["1.0.20", "1.0.26", "1.0.35", "1.0.36"])

    def test_an_unreadable_or_unknown_installed_list_moves_nothing(self):
        plugins, base = self.cache(("0.1.0", "1.0.18", "1.0.35", "1.0.36"))
        listed = os.path.join(plugins, "installed_plugins.json")
        self.assertEqual(ds.tidy_old_versions(base), [])  # missing
        for text in ("{not json", '{"plugins": []}', '{"plugins": {"other@x": []}}',
                     '{"plugins": {"datastash@datastash": [{"scope": "user"}]}}'):
            with self.subTest(text=text):
                with open(listed, "w") as f:
                    f.write(text)
                self.assertEqual(ds.tidy_old_versions(base), [])
        installed_plugins(plugins, [{"scope": "user", "version": "1.0.36"}])
        if os.geteuid() != 0:
            os.chmod(listed, 0)
            self.addCleanup(os.chmod, listed, 0o600)
            self.assertEqual(ds.tidy_old_versions(base), [])  # unreadable
        self.assertEqual(sorted(os.listdir(base)), ["0.1.0", "1.0.18", "1.0.35", "1.0.36"])
        self.assertFalse(os.path.exists(ds.VERSIONS_TRASH))

    def test_unused_old_versions_are_moved_to_the_trash_never_deleted(self):
        plugins = harness.tmpdir(self, dir=TMP)
        base = os.path.join(plugins, "cache", "datastash", "datastash")
        installed_plugins(plugins, [{"scope": "user", "version": "1.0.36"}])
        for v in ("0.1.0", "1.0.18", "1.0.26", "1.0.34", "1.0.35", "1.0.36"):
            os.makedirs(os.path.join(base, v, "scripts"))
            with open(os.path.join(base, v, "scripts", "datastash"), "w") as f:
                f.write(v)
        os.makedirs(os.path.join(base, "1.0.18", ".in_use"))
        open(os.path.join(base, "1.0.18", ".in_use", str(os.getpid())), "w").close()  # a running session uses it
        os.makedirs(os.path.join(base, "0.1.0", ".in_use"))
        open(os.path.join(base, "0.1.0", ".in_use", "999999999"), "w").close()  # a session long gone
        os.makedirs(os.path.join(base, "not-a-version"))
        self.addCleanup(setattr, ds, "VERSIONS_TRASH", ds.VERSIONS_TRASH)
        ds.VERSIONS_TRASH = os.path.join(harness.tmpdir(self, dir=TMP), "trash", "plugin-versions")
        moved = ds.tidy_old_versions(base)
        self.assertEqual(sorted(moved), ["0.1.0", "1.0.26", "1.0.34"])
        self.assertEqual(sorted(os.listdir(base)), ["1.0.18", "1.0.35", "1.0.36", "not-a-version"])
        trashed = sorted(os.listdir(ds.VERSIONS_TRASH))
        self.assertEqual([t.split("-")[0] for t in trashed], ["0.1.0", "1.0.26", "1.0.34"])
        for t in trashed:
            with open(os.path.join(ds.VERSIONS_TRASH, t, "scripts", "datastash")) as f:
                self.assertEqual(f.read(), t.split("-")[0])  # moved whole, nothing deleted
        self.assertEqual(ds.tidy_old_versions(base), [])

    def test_a_development_copy_touches_no_cache(self):
        self.assertFalse(ds.installed_from_marketplace())
        self.assertEqual(ds.tidy_old_versions(), [])


class ReleaseNotes(unittest.TestCase):
    def test_changelog_has_every_release_and_the_readme_links_it(self):
        with open(os.path.join(PLUGIN, "CHANGELOG.md")) as f:
            log = f.read()
        with open(os.path.join(HERE, "plugin-release.json")) as f:
            released = list(json.load(f)["versions"])
        for v in [v for v in released if ds.version_tuple(v) >= (1, 0, 26)] + ["1.0.36"]:
            with self.subTest(version=v):
                self.assertEqual(len(re.findall(r"(?m)^- \*\*" + re.escape(v) + r"\*\*: \S", log)), 1)
        self.assertNotIn("/mcp", log)
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        self.assertIn("(plugins/datastash/CHANGELOG.md)", readme)

    def test_whats_new_has_every_release_from_1_0_21(self):
        with open(os.path.join(HERE, "plugin-release.json")) as f:
            released = [v for v in json.load(f)["versions"] if ds.version_tuple(v) >= (1, 0, 21)]
        self.assertIn(ds.PLUGIN_VERSION, released)
        for v in released:
            with self.subTest(version=v):
                self.assertTrue(ds.WHATS_NEW.get(v), f"no what's-new line for {v}")
        for v, lines in ds.WHATS_NEW.items():
            for x in lines:
                self.assertNotIn("/mcp", x)
        self.assertIn("datastash remember", " ".join(ds.WHATS_NEW[ds.PLUGIN_VERSION]))
        self.assertIn("Function descriptions", " ".join(ds.WHATS_NEW[ds.PLUGIN_VERSION]))

    def test_readme_troubleshooting_has_no_mcp(self):
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        part = readme.split("### If datastash isn't waking you", 1)[1].split("### Fallback", 1)[0]
        self.assertNotIn("/mcp", part)


class GuardReadsPaths(OffBase):
    def denied(self, out):
        return bool(out) and out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def test_a_report_that_names_the_checker_or_token_files_is_written(self):
        ds.set_clean(commands=False)  # the token guard only, not the clean sessions refusal
        report = os.path.join(self.folder, "learnings", "review.md")
        text = ("Notes: the typed checker is datastash status-verify; the login keeps login.refresh, login.answer and "
                "login.access.json; answer-key; agent-answer-x.")
        for name, ti in (("Write", {"file_path": report, "content": text}),
                         ("Edit", {"file_path": report, "old_string": "a", "new_string": text}),
                         ("MultiEdit", {"file_path": report, "edits": [{"old_string": "a", "new_string": text}]}),
                         ("Grep", {"pattern": "status-verify", "path": self.folder})):
            with self.subTest(name=name):
                out = self.tool(name, tool_input=ti)
                self.assertFalse(self.denied(out) and out["hookSpecificOutput"]["permissionDecisionReason"] in (
                    ds.TOKEN_DENY, ds.PROOF_DENY), out)

    def test_real_reads_and_runs_are_still_refused(self):
        for name, ti in (("Read", {"file_path": os.path.join(ds.CONF, "login.refresh")}),
                         ("Read", {"file_path": os.path.join(ds.CONF, "login.answer")}),
                         ("Write", {"file_path": os.path.join(ds.CONF, "login.access.json"), "content": "x"}),
                         ("Glob", {"pattern": "**/login.refresh"}),
                         ("Grep", {"pattern": "x", "path": os.path.join(ds.CONF, "login.answer")}),
                         ("Bash", {"command": "datastash status-verify 0123456789ab"}),
                         ("Bash", {"command": "datastash token --header"})):
            with self.subTest(name=name, ti=ti):
                out = self.tool(name, tool_input=ti)
                self.assertTrue(self.denied(out), out)
                self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"], ds.TOKEN_DENY)
        out = self.tool("Read", tool_input={"file_path": os.path.join(ds.CONF, "agent-web-app.proof")})
        self.assertTrue(self.denied(out), out)


class ReviewFollowUps(Base):
    def test_the_check_email_line_is_not_repeated_by_the_model(self):
        for line in (ds.CHECK_EMAIL_LINE, ds.CHECKING_CODE_LINE):
            ai = ds.LOGIN_SHOWN_AI.format(line=line)
            self.assertIn(line, ai)
            self.assertIn("never repeat", ai)
            self.assertNotIn("whole reply is exactly", ai)
        self.addCleanup(setattr, ds, "start_check", ds.start_check)
        ds.start_check = lambda *a, **k: True
        line, ai, block = ds.login_step_prompt({"cwd": self.folder}, None, "datastash sync you@email.com")
        self.assertEqual((line, block), (ds.CHECK_EMAIL_LINE, False))
        self.assertEqual(ai, ds.LOGIN_SHOWN_AI.format(line=ds.CHECK_EMAIL_LINE))

    def test_the_busy_mark_keeps_its_first_time(self):
        rec = {"sid": "sess1", "kind": "sync_email", "tp": "/nope", "pid": "p1", "prompt": "x", "at": time.time()}
        seen = []
        real_write = ds.write_private

        def write(path, text):
            if path == ds._step_busy_path("sess1"):
                seen.append(json.loads(text)["at"])
            return real_write(path, text)
        for k in ("write_private", "find_typed", "_transcript_ok", "VERIFY_WAIT"):
            self.addCleanup(setattr, ds, k, getattr(ds, k))
        ds.write_private = write
        ds._transcript_ok = lambda tp, sid: True
        ds.find_typed = lambda *a, **k: (time.sleep(0.3), "not person")[1]
        real_exists = os.path.exists
        self.addCleanup(setattr, ds.os.path, "exists", real_exists)
        ds.os.path.exists = lambda p: True if p == "/nope" else real_exists(p)
        ds.login_step_verify(rec)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])

    def test_the_needs_auth_cache_is_read_before_the_sign_in_check(self):
        self.addCleanup(setattr, ds, "login_present", ds.login_present)
        asked = []
        ds.login_present = lambda: asked.append(1) or True
        cache = os.path.join(ds.CLAUDE_DIR, ds.NEEDS_AUTH_CACHE)
        os.makedirs(ds.CLAUDE_DIR, exist_ok=True)
        if os.path.exists(cache):
            os.replace(cache, cache + f".old-{time.time()}")
        self.assertFalse(ds.clear_connector_auth_mark())
        self.assertEqual(asked, [])  # no file: no sign-in check
        with open(cache, "w") as f:
            json.dump({"other": {"timestamp": 1}}, f)
        self.assertFalse(ds.clear_connector_auth_mark())
        self.assertEqual(asked, [])  # no mark of ours: no sign-in check
        with open(cache, "w") as f:
            json.dump({"other": {"timestamp": 1}, ds.CONNECTOR_SERVER_KEY: {"timestamp": 2}}, f)
        self.assertTrue(ds.clear_connector_auth_mark())
        self.assertEqual(asked, [1])
        with open(cache) as f:
            self.assertEqual(json.load(f), {"other": {"timestamp": 1}})

    def test_no_keychain_read_without_time_for_the_request(self):
        for k in ("_cached_access", "refresh_token", "LOGIN_DEADLINE"):
            self.addCleanup(setattr, ds, k, getattr(ds, k))
        ds.login_token = self.saved["login_token"]
        ds._cached_access = lambda: None
        read = []
        ds.refresh_token = lambda: read.append(1) or None
        ds.LOGIN_DEADLINE = ds.LOGIN_SAVE_RESERVE + 1  # under 1.5 s left from the start
        self.assertIsNone(ds.login_token(lock_wait=0.1))
        self.assertEqual(read, [])

    def test_new_session_marks_are_cleaned_up(self):
        ds.ensure_dirs()
        old = os.path.join(ds.STATE, "new-session-told-old1")
        fresh = os.path.join(ds.STATE, "new-session-told-fresh1")
        for p in (old, fresh):
            ds.write_private(p, "{}")
        past = time.time() - (ds.NEW_SESSION_MARK_DAYS + 1) * 86400
        os.utime(old, (past, past))
        ds.prune_new_session_marks()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        mine = os.path.join(ds.STATE, "new-session-told-sess9")
        ds.write_private(mine, "{}")
        self.addCleanup(setattr, ds, "hook_input", ds.hook_input)
        self.addCleanup(setattr, ds, "this_session", ds.this_session)
        ds.hook_input = lambda: {"session_id": "sess9", "hook_event_name": "SessionEnd", "cwd": self.folder}
        ds.this_session = lambda inp=None: {"sid": "sess9", "proc": None}
        self.run_cmd(ds.cmd_end, [])
        self.assertFalse(os.path.exists(mine))


if __name__ == "__main__":
    unittest.main()
