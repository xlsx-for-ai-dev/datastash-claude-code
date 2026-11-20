#!/usr/bin/env python3
"""Release check: a plugin that sets "version" in plugin.json stays pinned to that version in Claude Code ("Setting it
pins the plugin to that version until you change it"), so people who have it never get changed files under an old
version. test/plugin-release.json records the content fingerprint of every version. This fails when the plugin's files
change and the version does not (bump plugins/datastash/.claude-plugin/plugin.json and add the new version's line), or
when a recorded version's files change.
Run: python3 -I test/test_release.py"""
import hashlib
import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "..", "plugins", "datastash")
RECORD = os.path.join(HERE, "plugin-release.json")


def fingerprint(root=PLUGIN):
    """sha256 over every file under the plugin folder: its path, its mode's executable bit and its bytes."""
    h = hashlib.sha256()
    files = []
    for d, dirs, names in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        for n in names:
            if n.endswith(".pyc"):
                continue
            files.append(os.path.relpath(os.path.join(d, n), root).replace(os.sep, "/"))
    for rel in sorted(files):
        p = os.path.join(root, rel)
        with open(p, "rb") as f:
            data = f.read()
        h.update(rel.encode() + b"\0" + (b"x" if os.access(p, os.X_OK) else b"-") + b"\0")
        h.update(hashlib.sha256(data).digest())
    return h.hexdigest()


class Release(unittest.TestCase):
    def test_changed_plugin_files_need_a_new_version(self):
        with open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json")) as f:
            version = json.load(f)["version"]
        with open(RECORD) as f:
            record = json.load(f)["versions"]
        now = fingerprint()
        self.assertIn(version, record, f"version {version} is not in test/plugin-release.json; add "
                                       f'"{version}": "{now}" (and never edit an earlier line)')
        same = [v for v, fp in record.items() if fp == now and v != version]
        self.assertEqual(same, [], f"these files were already released as {same}")
        self.assertEqual(record[version], now,
                         f"the plugin's files changed but plugin.json still says {version}: raise the version in "
                         f'plugins/datastash/.claude-plugin/plugin.json and add its line to test/plugin-release.json '
                         f'(fingerprint {now})')

    def test_versions_only_go_up(self):
        with open(RECORD) as f:
            versions = list(json.load(f)["versions"])
        nums = [tuple(int(x) for x in v.split(".")) for v in versions]
        self.assertEqual(nums, sorted(nums))
        self.assertEqual(len(set(nums)), len(nums))


if __name__ == "__main__":
    unittest.main()
