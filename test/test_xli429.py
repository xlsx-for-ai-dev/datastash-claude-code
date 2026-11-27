#!/usr/bin/env python3
"""Offline tests for XLI-429 (plugin 1.0.27): installs stayed on an old version.
- The self-update hook starts its pass on its own and returns at once, so a short `claude -p` run that ends (and stops
  its async hooks) no longer cuts the check off mid-way; an unfinished check ("checking") holds the day for 15 minutes
  only, not 24 hours.
- `datastash doctor` names the version this session runs, the one installed on disk, and the newest published one
  (the public catalog's pin), and when this computer is behind says so with the one fix line.
No network, no Claude Code run; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_xli429.py"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import ds, TMP  # noqa: E402  (the same harness and plugin copy)
from test_selfupdate import Base, catalog, tags, A, B  # noqa: E402
import harness  # noqa: E402,F401

FIX = "claude plugin marketplace update datastash && claude plugin update datastash@datastash"


class StaleCheck(Base):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.patch("PLUGIN_VERSION", "1.0.0")
        self.patch("installed_from_marketplace", lambda root=None: True)
        self.patch("_ls_remote", lambda repo=ds.RELEASE_REPO: self.calls.append("ls-remote") or tags((A, "refs/tags/v1.0.0")))
        self.patch("_run_claude", lambda args: self.calls.append(" ".join(args)) or 0)
        self.patch("marketplace_catalog", lambda: None)

    def write_state(self, st):
        ds.ensure_dirs()
        ds.write_private(ds.SELF_UPDATE_FILE, json.dumps(st))

    def test_unfinished_check_is_tried_again_after_15_minutes(self):
        self.write_state({"checked_at": 1000, "result": "checking"})
        self.assertEqual(ds.self_update(now=1000 + ds.SELF_UPDATE_STALE - 1), "recent")
        self.assertEqual(ds.self_update(now=1000 + ds.SELF_UPDATE_STALE), "current")
        self.assertEqual(self.calls, ["ls-remote"])

    def test_finished_check_still_holds_the_day(self):
        self.write_state({"checked_at": 1000, "result": "current"})
        self.assertEqual(ds.self_update(now=1000 + ds.SELF_UPDATE_STALE + 60), "recent")
        self.assertEqual(self.calls, [])


class HookStartsOnItsOwn(Base):
    def test_hook_starts_the_pass_detached_and_returns(self):
        started, ran = [], []
        self.patch("hook_input", lambda: {})
        self.patch("spawn_detached", lambda argv, cwd: started.append(argv[-2:]))
        self.patch("self_update", lambda now=None: ran.append(1))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ds.cmd_self_update([]), 0)
        self.assertEqual((started, ran, out.getvalue()), ([["self-update", "--here"]], [], ""))

    def test_runs_here_when_it_cannot_start_on_its_own(self):
        ran = []
        self.patch("hook_input", lambda: {})
        self.patch("spawn_detached", lambda argv, cwd: (_ for _ in ()).throw(OSError("no")))
        self.patch("self_update", lambda now=None: ran.append(1))
        self.assertEqual(ds.cmd_self_update([]), 0)
        self.assertEqual(ran, [1])


class VersionReport(unittest.TestCase):
    def test_current(self):
        self.assertEqual(ds.version_report("1.0.27", "1.0.27", "1.0.27"),
                         [("ok", "version: this session runs datastash 1.0.27; installed: 1.0.27; newest: 1.0.27.")])

    def test_behind_says_so_with_the_one_fix_line(self):
        got = ds.version_report("1.0.27", "1.0.24", "1.0.24")
        self.assertEqual([k for k, _ in got], ["note", "problem", "fix"])
        self.assertIn("this computer is behind (datastash 1.0.24, newest 1.0.27)", got[1][1])
        self.assertIn("restart Claude Code", got[1][1])
        self.assertEqual(got[2], ("fix", FIX))

    def test_installed_newer_than_running_says_restart(self):
        got = ds.version_report("1.0.27", "1.0.27", "1.0.24")
        self.assertEqual(got, [("note", "version: this session runs datastash 1.0.24; installed: 1.0.27; newest: "
                                        "1.0.27. Restart Claude Code to finish the update.")])

    def test_newest_unknown_is_only_a_note(self):
        got = ds.version_report(None, "1.0.24", "1.0.24")
        self.assertEqual([k for k, _ in got], ["note"])
        self.assertIn("could not be read", got[0][1])


class Published(unittest.TestCase):
    def patch(self, name, value):
        old = getattr(ds, name)
        setattr(ds, name, value)
        self.addCleanup(setattr, ds, name, old)

    def test_no_network_in_tests(self):
        self.assertIsNone(ds.published_version())

    def fake(self, body):
        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        self.addCleanup(os.environ.__setitem__, "DATASTASH_NO_NETWORK", os.environ.get("DATASTASH_NO_NETWORK", "1"))
        os.environ["DATASTASH_NO_NETWORK"] = "0"
        self.patch("urllib", type("U", (), {"request": type("Q", (), {
            "Request": staticmethod(lambda url, headers=None: url),
            "urlopen": staticmethod(lambda req, timeout=None: R(body))}), "error": ds.urllib.error}))

    def test_reads_the_catalog_pin(self):
        self.fake(json.dumps(catalog("v1.0.27", B)).encode())
        self.assertEqual(ds.published_version(), "1.0.27")

    def test_a_branch_pin_or_junk_is_none(self):
        self.fake(json.dumps(catalog("main", B)).encode())
        self.assertIsNone(ds.published_version())
        self.fake(b"not json")
        self.assertIsNone(ds.published_version())


class Installed(unittest.TestCase):
    def test_reads_installed_plugins_beside_the_cache(self):
        plugins = os.path.join(TMP, "xli429", ".claude", "plugins")
        root = os.path.join(plugins, "cache", "datastash", "datastash", "1.0.24")
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(plugins, "installed_plugins.json"), "w") as f:
            json.dump({"version": 2, "plugins": {"datastash@datastash": [
                {"scope": "user", "version": "1.0.27"}, {"scope": "project", "version": "1.0.24"}]}}, f)
        self.assertEqual(ds.installed_version(root), "1.0.27")
        os.replace(os.path.join(plugins, "installed_plugins.json"), os.path.join(TMP, "xli429-moved.json"))
        self.assertIsNone(ds.installed_version(root))


if __name__ == "__main__":
    unittest.main()
