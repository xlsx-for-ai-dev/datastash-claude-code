#!/usr/bin/env python3
"""Offline checks of the skill text (owner point 11-20): the always-loaded description stays under Claude Code's
1,536-character listing cap with the trigger words first, and the skill body carries the Claude Code detail that the
connector's short, client-neutral server instructions no longer hold.
Run: python3 -I test/test_skill.py"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.join(HERE, "..", "plugins", "datastash", "skills", "datastash-agent", "SKILL.md")


def parts():
    with open(SKILL) as f:
        text = f.read()
    _, head, body = text.split("---", 2)
    m = re.search(r"^description: (.*)$", head, re.M)
    return m.group(1), body


class SkillText(unittest.TestCase):
    def test_description_is_short_and_plain_yaml(self):
        desc, _ = parts()
        self.assertLess(len(desc), 1536)
        self.assertNotIn(": ", desc)  # a plain YAML scalar breaks on ": "
        self.assertNotIn(" #", desc)

    def test_trigger_words_come_first(self):
        desc, _ = parts()
        first = desc[:300]
        for phrase in ("datastash", '"sync datastash"', '"datastash sync"', '"datastash main"', '"main datastash"', '"save this"', '"track my leads"'):
            self.assertIn(phrase, first, phrase)

    def test_body_carries_the_claude_code_detail(self):
        _, body = parts()
        section = body.split("## datastash in Claude Code")[1].split("\n## ")[0]
        for needle in ('save only when the person asks, until they type "sync datastash"', "tell them in a few words",
                       "delete anything saved", "ledger_upload_link", "ledger_export", 'topic "formulas"', 'topic "cursor"',
                       "never add datastash to one on your own"):
            self.assertIn(needle, section, needle)
        self.assertIn("type /mcp, pick datastash, choose Authenticate, and sign in with Google", body)


if __name__ == "__main__":
    unittest.main()
