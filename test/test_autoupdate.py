#!/usr/bin/env python3
"""Offline tests for plugin 1.0.17: /datastash:install turns on Claude Code's own auto-update for the datastash
marketplace (what the /plugin Marketplaces toggle writes: known_marketplaces.json, and the user settings'
extraKnownMarketplaces entry only when it is already declared there), says "Auto-update is on for datastash.", records
what it changed, and the uninstall of the last agent on this computer puts back exactly that (a value the person set is
left alone; no record: left as it was). Paths: no settings file, settings with other
marketplaces (only datastash's entry changes, formatting kept, a backup, atomic), already on, and the person's settings
turning updates off (nothing written, the fixed line).
No network, no Claude Code run; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_autoupdate.py"""
import json
import os
import stat
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import ds, TMP  # noqa: E402  (the same harness and plugin copy)
import harness  # noqa: E402

ENVS = ("DISABLE_UPDATES", "DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "FORCE_AUTOUPDATE_PLUGINS",
        "CLAUDE_CODE_PLUGIN_CACHE_DIR")
SOURCE = {"source": "github", "repo": "xlsx-for-ai-dev/datastash-claude-code"}
OTHER = {"source": {"source": "github", "repo": "example-org/other-plugins"}, "autoUpdate": False}

SETTINGS_TEXT = """{
    "permissions": {
        "allow": ["Bash(ls:*)"],
        "deny": []
    },
    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]},
    "extraKnownMarketplaces": {
        "other": {"source": {"source": "github", "repo": "example-org/other-plugins"}, "autoUpdate": false},
        "datastash": {
            "source": {"source": "github", "repo": "xlsx-for-ai-dev/datastash-claude-code"}
        }
    },
    "env": {"SOME_SETTING": "x"}
}
"""


def known(entry_extra=None, others=True):
    d = {"datastash": dict({"source": SOURCE, "installLocation": "/nowhere/marketplaces/datastash",
                            "lastUpdated": "2026-11-25T00:00:00.000Z"}, **(entry_extra or {}))}
    if others:
        d["other"] = dict(OTHER, installLocation="/nowhere/marketplaces/other")
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.claude = os.path.join(TMP, f"claude-{self.id().rsplit('.', 1)[-1]}")
        self.plugins = os.path.join(self.claude, "plugins")
        os.makedirs(self.plugins)
        old = ds.CLAUDE_DIR
        ds.CLAUDE_DIR = self.claude
        self.addCleanup(setattr, ds, "CLAUDE_DIR", old)
        for k in ENVS:
            if k in os.environ:
                v = os.environ.pop(k)
                self.addCleanup(os.environ.__setitem__, k, v)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ENVS])
        for name, leaf in (("AUTOUPDATE_STATE", "autoupdate.json"), ("AUTOUPDATE_ASIDE", "autoupdate-removed")):
            self.addCleanup(setattr, ds, name, getattr(ds, name))
            setattr(ds, name, os.path.join(self.claude, "state", leaf))
        self.settings = os.path.join(self.claude, "settings.json")
        self.known = os.path.join(self.plugins, "known_marketplaces.json")

    def write(self, path, data, mode=0o600):
        with open(path, "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data, indent=2))
        os.chmod(path, mode)

    def read(self, path):
        with open(path) as f:
            return f.read()


class TurnOn(Base):
    def test_no_settings_file_only_the_known_marketplaces_entry_changes(self):
        self.write(self.known, known())
        self.assertEqual(ds.set_marketplace_autoupdate(True), "on")
        self.assertFalse(os.path.exists(self.settings), "no settings file is made")
        got = json.loads(self.read(self.known))
        self.assertIs(got["datastash"]["autoUpdate"], True)
        self.assertEqual(got["other"], dict(OTHER, installLocation="/nowhere/marketplaces/other"), "never another one")
        want = known()
        want["datastash"]["autoUpdate"] = True
        self.assertEqual(got, want, "nothing else changes")
        self.assertIs(ds.marketplace_autoupdate(), True)

    def test_settings_with_other_marketplaces_change_only_the_datastash_entry_formatting_kept(self):
        self.write(self.settings, SETTINGS_TEXT, 0o640)
        self.write(self.known, known())
        self.assertEqual(ds.set_marketplace_autoupdate(True), "on")
        text = self.read(self.settings)
        want = SETTINGS_TEXT.replace(
            '"datastash": {\n            "source"', '"datastash": {\n            "autoUpdate": true,\n            "source"')
        self.assertEqual(text, want, "only one member added, in the file's own indent")
        d = json.loads(text)
        self.assertEqual(d["permissions"], {"allow": ["Bash(ls:*)"], "deny": []})
        self.assertIs(d["extraKnownMarketplaces"]["other"]["autoUpdate"], False, "another marketplace stays off")
        self.assertEqual(stat.S_IMODE(os.stat(self.settings).st_mode), 0o640, "the file's mode is kept")
        self.assertEqual(self.read(self.settings + ".datastash-backup"), SETTINGS_TEXT, "a backup of the file as it was")
        self.assertEqual(stat.S_IMODE(os.stat(self.settings + ".datastash-backup").st_mode), 0o640)
        self.assertIs(json.loads(self.read(self.known))["datastash"]["autoUpdate"], True, "the toggle's file too")
        self.assertEqual([n for n in os.listdir(self.claude) if n.endswith(".tmp")], [], "no temp file left")

    def test_settings_not_declaring_datastash_are_not_touched(self):
        text = '{"permissions": {"allow": []}, "extraKnownMarketplaces": {"other": {"source": {"source": "github", ' \
               '"repo": "example-org/other-plugins"}}}}\n'
        self.write(self.settings, text)
        self.write(self.known, known())
        self.assertEqual(ds.set_marketplace_autoupdate(True), "on")
        self.assertEqual(self.read(self.settings), text)
        self.assertFalse(os.path.exists(self.settings + ".datastash-backup"))

    def test_already_on_writes_nothing(self):
        self.write(self.known, known({"autoUpdate": True}))
        before = os.stat(self.known).st_mtime_ns
        self.assertEqual(ds.set_marketplace_autoupdate(True), "on")
        self.assertEqual(os.stat(self.known).st_mtime_ns, before)
        self.assertFalse(os.path.exists(self.known + ".datastash-backup"))

    def test_a_false_value_in_place_is_flipped_in_place(self):
        self.write(self.known, known({"autoUpdate": False}))
        self.assertEqual(ds.set_marketplace_autoupdate(True), "on")
        self.assertIs(json.loads(self.read(self.known))["datastash"]["autoUpdate"], True)
        self.assertIs(json.loads(self.read(self.known))["other"]["autoUpdate"], False)

    def test_settings_entry_off_wins_so_it_is_flipped_there(self):
        self.write(self.settings, {"extraKnownMarketplaces": {"datastash": {"source": SOURCE, "autoUpdate": False}}})
        self.write(self.known, known({"autoUpdate": True}))
        self.assertIs(ds.marketplace_autoupdate(), False, "the settings entry is read first")
        self.assertEqual(ds.set_marketplace_autoupdate(True), "on")
        self.assertIs(json.loads(self.read(self.settings))["extraKnownMarketplaces"]["datastash"]["autoUpdate"], True)

    def test_a_symlinked_settings_file_is_written_at_its_target(self):
        target = os.path.join(TMP, f"dotfiles-{os.getpid()}-settings.json")
        self.write(target, SETTINGS_TEXT)
        os.symlink(target, self.settings)
        self.write(self.known, known())
        self.assertEqual(ds.set_marketplace_autoupdate(True), "on")
        self.assertTrue(os.path.islink(self.settings), "the link stays a link")
        self.assertIs(json.loads(self.read(target))["extraKnownMarketplaces"]["datastash"]["autoUpdate"], True)

    def test_no_datastash_marketplace_is_missing_and_shows_no_line(self):
        self.write(self.known, {"other": OTHER})
        self.assertEqual(ds.set_marketplace_autoupdate(True), "missing")
        self.assertIsNone(ds.autoupdate_install_line())
        self.assertNotIn("autoUpdate", json.loads(self.read(self.known))["other"].keys() - {"autoUpdate"})

    def test_a_file_that_is_not_plain_json_is_left_alone(self):
        text = '{\n  // a comment\n  "extraKnownMarketplaces": {"datastash": {"source": {}}}\n}\n'
        self.write(self.settings, text)
        self.write(self.known, known())
        self.assertEqual(ds.set_marketplace_autoupdate(True), "on", "the toggle's own file still turns it on")
        self.assertEqual(self.read(self.settings), text)

    def test_the_plugins_folder_follows_the_cache_dir_setting(self):
        alt = os.path.join(TMP, f"alt-plugins-{os.getpid()}")
        os.makedirs(alt)
        os.environ["CLAUDE_CODE_PLUGIN_CACHE_DIR"] = alt
        self.write(os.path.join(alt, "known_marketplaces.json"), known())
        self.assertEqual(ds.set_marketplace_autoupdate(True), "on")
        self.assertIs(json.loads(self.read(os.path.join(alt, "known_marketplaces.json")))["datastash"]["autoUpdate"],
                      True)

    def test_an_installed_copy_uses_the_plugins_folder_it_was_installed_in(self):
        root = os.path.join(self.plugins, "cache", "datastash", "datastash", "1.0.17")
        self.assertEqual(ds.claude_plugins_dir(root), os.path.realpath(self.plugins))


class Unwritable(Base):
    """Some computers have a root-owned ~/.claude/settings.json: never replaced; the next-best scope is used and named."""

    def setUp(self):
        super().setUp()
        self.folder = os.path.join(TMP, f"proj-{self.id().rsplit('.', 1)[-1]}")
        os.makedirs(self.folder)
        self.local = os.path.join(self.folder, ".claude", "settings.local.json")
        if os.geteuid() == 0:
            self.skipTest("root can write any file")

    def locked(self, path, data):
        self.write(path, data, 0o444)
        self.addCleanup(os.chmod, path, 0o600)

    def test_unwritable_settings_declaring_it_off_turn_it_on_in_this_folder_only(self):
        self.locked(self.settings, {"extraKnownMarketplaces": {"datastash": {"source": SOURCE, "autoUpdate": False}},
                                    "permissions": {"allow": []}})
        before = self.read(self.settings)
        self.write(self.known, known())
        self.assertEqual(ds.set_marketplace_autoupdate(True, self.folder), "folder")
        self.assertEqual(self.read(self.settings), before, "the root-owned file is never replaced")
        self.assertFalse(os.path.exists(self.settings + ".datastash-backup"))
        local = json.loads(self.read(self.local))
        self.assertEqual(local, {"extraKnownMarketplaces": {"datastash": {"source": SOURCE, "autoUpdate": True}}})
        self.assertEqual(stat.S_IMODE(os.stat(self.local).st_mode), 0o600)
        self.assertEqual(ds.autoupdate_install_line(self.folder), "Auto-update is on for datastash in this folder only.")

    def test_an_existing_local_settings_file_keeps_everything_else(self):
        self.locked(self.settings, {"extraKnownMarketplaces": {"datastash": {"source": SOURCE, "autoUpdate": False}}})
        self.write(self.known, known())
        os.makedirs(os.path.dirname(self.local))
        text = '{\n  "permissions": {"allow": ["Read"]}\n}\n'
        self.write(self.local, text)
        self.assertEqual(ds.set_marketplace_autoupdate(True, self.folder), "folder")
        got = json.loads(self.read(self.local))
        self.assertEqual(got["permissions"], {"allow": ["Read"]})
        self.assertIs(got["extraKnownMarketplaces"]["datastash"]["autoUpdate"], True)
        self.assertEqual(self.read(self.local + ".datastash-backup"), text)

    def test_unwritable_settings_without_a_value_still_turn_it_on_through_the_toggle_store(self):
        self.locked(self.settings, {"extraKnownMarketplaces": {"datastash": {"source": SOURCE}}})
        self.write(self.known, known())
        self.assertEqual(ds.set_marketplace_autoupdate(True, self.folder), "on")
        self.assertFalse(os.path.exists(self.local), "no folder file when the computer-wide one worked")

    def test_nothing_writable_says_so_and_never_fails_the_install(self):
        self.locked(self.settings, {"extraKnownMarketplaces": {"datastash": {"source": SOURCE, "autoUpdate": False}}})
        self.locked(self.known, known())
        os.chmod(self.folder, 0o555)
        self.addCleanup(os.chmod, self.folder, 0o700)
        self.assertEqual(ds.autoupdate_install_line(self.folder),
                         "Couldn't turn on auto-update here; datastash still checks for updates when a session starts.")
        lst = {"agents": [{"id": "a" * 32, "name": "web-app", "on": True, "shut": False}]}
        text = ds.install_text({"cwd": self.folder}, {"name": "web-app"}, lst)
        self.assertTrue(text.startswith("Done. Datastash is on for web-app."), text)
        self.assertIn("\nCouldn't turn on auto-update here;", text)

    def test_the_last_uninstall_takes_out_the_folder_file_install_made(self):
        self.locked(self.settings, {"extraKnownMarketplaces": {"datastash": {"source": SOURCE, "autoUpdate": False}}})
        self.write(self.known, known())
        before = self.read(self.known)
        self.assertEqual(ds.set_marketplace_autoupdate(True, self.folder), "folder")
        self.assertTrue(os.path.exists(self.local))
        self.assertEqual(ds.restore_marketplace_autoupdate(), "off")
        self.assertEqual(self.read(self.known), before)
        self.assertFalse(os.path.lexists(self.local), "the file install made is gone from the folder")
        self.assertFalse(os.path.lexists(os.path.dirname(self.local)), "and the .claude folder it made")
        self.assertEqual(len([n for n in os.listdir(ds.AUTOUPDATE_ASIDE) if n.endswith("-settings.local.json")]), 1,
                         "moved aside, never deleted")
        self.assertEqual(os.listdir(self.folder), [])

    def test_the_last_uninstall_takes_out_only_the_entry_install_added_to_a_folder_file(self):
        self.locked(self.settings, {"extraKnownMarketplaces": {"datastash": {"source": SOURCE, "autoUpdate": False}}})
        self.write(self.known, known())
        os.makedirs(os.path.dirname(self.local))
        for text in ('{\n  "permissions": {"allow": ["Read"]}\n}\n',
                     '{\n  "extraKnownMarketplaces": {"other": {"source": "x"}},\n  "env": {}\n}\n'):
            with self.subTest(text):
                self.write(self.local, text)
                self.assertEqual(ds.set_marketplace_autoupdate(True, self.folder), "folder")
                self.assertNotEqual(self.read(self.local), text)
                ds.restore_marketplace_autoupdate()
                self.assertEqual(self.read(self.local), text, "the person's file, byte for byte")
                self.assertFalse(os.path.exists(self.local + ".datastash-backup"), "the backup install made is moved aside")


class EnvOff(Base):
    def test_each_variable_keeps_it_off_and_writes_nothing(self):
        for k in ("DISABLE_UPDATES", "DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"):
            with self.subTest(k):
                self.write(self.known, known())
                before = self.read(self.known)
                os.environ[k] = "1"
                try:
                    self.assertEqual(ds.set_marketplace_autoupdate(True), "env")
                    self.assertEqual(ds.autoupdate_install_line(),
                                     "Auto-update is turned off on this computer by your settings.")
                    self.assertEqual(self.read(self.known), before, "never fought")
                finally:
                    os.environ.pop(k)

    def test_the_settings_env_block_counts_too(self):
        self.write(self.settings, {"env": {"DISABLE_AUTOUPDATER": "true"}})
        self.write(self.known, known())
        self.assertEqual(ds.autoupdate_install_line(), ds.AUTOUPDATE_ENV_LINE)

    def test_force_and_falsy_values_do_not_count(self):
        self.write(self.known, known())
        os.environ["DISABLE_AUTOUPDATER"] = "0"
        self.assertFalse(ds.autoupdate_env_off())
        os.environ["DISABLE_AUTOUPDATER"] = "1"
        os.environ["FORCE_AUTOUPDATE_PLUGINS"] = "1"
        self.assertFalse(ds.autoupdate_env_off())
        self.assertEqual(ds.autoupdate_install_line(), "Auto-update is on for datastash.")


class InstallAndUninstall(Base):
    def test_already_on_before_install_the_last_uninstall_says_left(self):
        self.write(self.known, known({"autoUpdate": True}))
        before = self.read(self.known)
        ds.set_marketplace_autoupdate(True)
        old = ds.all_agents
        ds.all_agents = lambda: []
        self.addCleanup(setattr, ds, "all_agents", old)
        self.assertTrue(ds.uninstall_line(["web-app"]).endswith(" Auto-update for datastash left as it was."))
        self.assertEqual(self.read(self.known), before)


    def setUp(self):
        super().setUp()
        self.write(self.known, known())
        self.agents = []

    def fake_agents(self, names):
        self.agents = [{"slug": n, "name": n, "workbook_id": "w"} for n in names]
        old = ds.all_agents
        ds.all_agents = lambda: list(self.agents)
        self.addCleanup(setattr, ds, "all_agents", old)

    def test_install_text_shows_the_line_under_done(self):
        lst = {"agents": [{"id": "a" * 32, "name": "web-app", "on": True, "shut": False}]}
        text = ds.install_text({}, {"name": "web-app"}, lst)
        lines = text.split("\n")
        self.assertTrue(lines[0].startswith("Done. Datastash is on for web-app."), lines[0])
        self.assertEqual(lines[1], "Auto-update is on for datastash.")
        self.assertIs(ds.marketplace_autoupdate(), True)

    def test_uninstall_of_the_last_agent_here_puts_it_back_and_says_so(self):
        before = self.read(self.known)
        ds.set_marketplace_autoupdate(True)
        self.fake_agents([])
        line = ds.uninstall_line(["web-app"])
        self.assertTrue(line.startswith("Done. Datastash is uninstalled from"), line)
        self.assertTrue(line.endswith(" Auto-update is off for datastash."), line)
        self.assertEqual(self.read(self.known), before)
        self.assertIs(ds.marketplace_autoupdate(), False)

    def test_uninstall_with_no_record_leaves_it_and_says_so(self):
        self.write(self.known, known({"autoUpdate": True}))
        before = self.read(self.known)
        self.fake_agents([])
        line = ds.uninstall_line(["web-app"])
        self.assertTrue(line.endswith(" Auto-update for datastash left as it was."), line)
        self.assertEqual(self.read(self.known), before)

    def test_a_partial_uninstall_leaves_it_on(self):
        ds.set_marketplace_autoupdate(True)
        self.fake_agents(["notes"])
        line = ds.uninstall_line(["web-app"])
        self.assertNotIn("Auto-update", line)
        self.assertIs(ds.marketplace_autoupdate(), True)

    def test_nothing_removed_changes_nothing(self):
        ds.set_marketplace_autoupdate(True)
        self.fake_agents([])
        self.assertNotIn("Auto-update", ds.uninstall_line([], ["web-app"], "datastash refused it"))
        self.assertIs(ds.marketplace_autoupdate(), True)


class Restore(Base):
    """Uninstall of the last agent puts back exactly what install changed (and nothing the person set)."""

    def test_already_on_before_install_stays_on(self):
        self.write(self.known, known({"autoUpdate": True}))
        before = self.read(self.known)
        self.assertEqual(ds.set_marketplace_autoupdate(True), "on")
        self.assertEqual(ds.restore_marketplace_autoupdate(), "left")
        self.assertEqual(self.read(self.known), before)
        self.assertIs(ds.marketplace_autoupdate(), True)

    def test_off_before_install_goes_back_to_false(self):
        self.write(self.known, known({"autoUpdate": False}))
        before = self.read(self.known)
        self.assertEqual(ds.set_marketplace_autoupdate(True), "on")
        self.assertIs(ds.marketplace_autoupdate(), True)
        self.assertEqual(ds.restore_marketplace_autoupdate(), "off")
        self.assertEqual(self.read(self.known), before)

    def test_key_absent_before_install_is_taken_out(self):
        self.write(self.known, known())
        before = self.read(self.known)
        self.write(self.settings, SETTINGS_TEXT)
        self.assertEqual(ds.set_marketplace_autoupdate(True), "on")
        self.assertIn('"autoUpdate": true', self.read(self.known))
        self.assertIn('"autoUpdate": true', self.read(self.settings))
        self.assertEqual(ds.restore_marketplace_autoupdate(), "off")
        self.assertEqual(self.read(self.known), before, "the file's own bytes, no autoUpdate key left")
        self.assertEqual(self.read(self.settings), SETTINGS_TEXT)
        self.assertFalse(os.path.exists(self.known + ".datastash-backup"))
        self.assertFalse(os.path.exists(self.settings + ".datastash-backup"))

    def test_a_second_install_keeps_the_first_record(self):
        self.write(self.known, known())
        before = self.read(self.known)
        ds.set_marketplace_autoupdate(True)
        self.assertEqual(ds.set_marketplace_autoupdate(True), "on")
        self.assertEqual(ds.restore_marketplace_autoupdate(), "off")
        self.assertEqual(self.read(self.known), before)

    def test_a_value_the_person_changed_since_is_left_alone(self):
        self.write(self.known, known())
        ds.set_marketplace_autoupdate(True)
        text = self.read(self.known).replace('"autoUpdate": true', '"autoUpdate": false')
        self.write(self.known, text)
        self.assertEqual(ds.restore_marketplace_autoupdate(), "left")
        self.assertEqual(self.read(self.known), text)

    def test_missing_record_leaves_it_as_it_is(self):
        for v in (True, False):
            with self.subTest(v):
                self.write(self.known, known({"autoUpdate": v}))
                before = self.read(self.known)
                self.assertEqual(ds.restore_marketplace_autoupdate(), "norecord")
                self.assertEqual(self.read(self.known), before)
        os.makedirs(os.path.dirname(ds.AUTOUPDATE_STATE), exist_ok=True)
        self.write(ds.AUTOUPDATE_STATE, {"folders": ["/somewhere"]})  # the unreleased 1.0.17 candidate's shape
        self.assertEqual(ds.restore_marketplace_autoupdate(), "norecord")

    def test_the_record_holds_no_secret_and_is_private(self):
        self.write(self.known, known())
        ds.set_marketplace_autoupdate(True)
        rec = json.loads(self.read(ds.AUTOUPDATE_STATE))
        self.assertEqual(rec["v"], 2)
        self.assertEqual([(c["file"], c["keys"], c["had"]) for c in rec["changes"]], [(self.known, ["datastash"], False)])
        self.assertEqual(stat.S_IMODE(os.stat(ds.AUTOUPDATE_STATE).st_mode), 0o600)

    def test_no_record_no_change(self):
        self.write(self.known, known())
        before = self.read(self.known)
        os.makedirs(os.path.dirname(ds.AUTOUPDATE_STATE), exist_ok=True)
        old = ds.write_private
        ds.write_private = lambda *a: (_ for _ in ()).throw(OSError("full"))
        self.addCleanup(setattr, ds, "write_private", old)
        self.assertEqual(ds.set_marketplace_autoupdate(True, "/nonexistent-folder"), "failed")
        self.assertEqual(self.read(self.known), before, "what can't be recorded is never changed")


class JsonEdit(unittest.TestCase):
    def test_delete_is_the_inverse_of_set(self):
        for t, path in (('{"datastash":{"x":1}}', ["datastash"]), ('{"datastash":{}}', ["datastash"]),
                        ('{\n  "a": {\n    "x": 1,\n    "y": [2]\n  }\n}\n', ["a"]), ("{}\n", [])):
            with self.subTest(t):
                self.assertEqual(ds.json_del(ds.json_set_flag(t, path, "autoUpdate", True), path, "autoUpdate"), t)
        self.assertEqual(ds.json_del('{"a": 1, "b": 2}', [], "b"), '{"a": 1}')
        self.assertIsNone(ds.json_del('{"a": 1}', [], "b"))

    def test_compact_and_nested_text(self):
        t = '{"a":{"datastash":{"x":1}},"b":[1,{"datastash":{}}]}'
        self.assertEqual(ds.json_set_flag(t, ["a", "datastash"], "autoUpdate", True),
                         '{"a":{"datastash":{"autoUpdate": true,"x":1}},"b":[1,{"datastash":{}}]}')
        self.assertEqual(ds.json_set_flag('{"datastash":{}}', ["datastash"], "autoUpdate", False),
                         '{"datastash":{"autoUpdate": false}}')
        self.assertIsNone(ds.json_set_flag('{"datastash": 1}', ["datastash"], "autoUpdate", True))
        self.assertIsNone(ds.json_set_flag('[]', ["datastash"], "autoUpdate", True))
        self.assertIsNone(ds.json_set_flag('{"a": "}"', ["a"], "autoUpdate", True))

    def test_strings_with_braces_and_escapes(self):
        t = '{"s": "{\\"datastash\\": {}}", "datastash": {"k": "a,}b", "autoUpdate": false}}'
        got = ds.json_set_flag(t, ["datastash"], "autoUpdate", True)
        self.assertEqual(got, t.replace('"autoUpdate": false', '"autoUpdate": true'))


if __name__ == "__main__":
    unittest.main(verbosity=1)
