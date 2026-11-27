#!/usr/bin/env python3
"""Offline tests for XLI-426 experience-based time estimates (plugin 1.0.25): `datastash estimate`.
- One best estimate per card: the recency-weighted median of the account's own finished cards (never the card itself,
  never "no card", nothing under EST_MIN_CARD_MINUTES); --like narrows to the named cards; no history still answers.
- A subagent's card comes from its description, else the id written most often in capitals in its first prompt.
- est_record writes a card's estimate once and later only refreshes its actual.
- The prompt hook: the person's typed "DDS estimate XLI-12" (any card), the how-long rule, the build-list rule, and the
  one-time tracker yes/no (recorded only while asked and unanswered).
No network, no key; under test/harness.py.
Run: python3 -I test/test_estimates.py"""
import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import ds, TMP  # noqa: E402  (the same harness and plugin copy)
import harness  # noqa: E402

NOW = 1_800_000_000.0
DAY = 86400.0


def h(minutes, days_ago):
    return {"minutes": float(minutes), "last": NOW - days_ago * DAY}


class Estimate(unittest.TestCase):
    def test_weighted_mean_of_the_other_finished_cards(self):
        hist = {"XLI-1": h(60, 0), "XLI-2": h(180, 0), "XLI-3": h(150, 0), "XLI-9": h(999, 0), "no card": h(5000, 0),
                "XLI-4": h(3, 0)}
        m, n = ds.est_estimate(hist, "XLI-9", now=NOW)
        self.assertEqual((m, n), (130, 3))  # XLI-9 itself, "no card" and the 3-minute card are left out

    def test_newer_cards_weigh_more(self):
        hist = {"A-1": h(60, 28), "A-2": h(60, 28), "A-3": h(200, 0)}
        self.assertEqual(ds.est_estimate(hist, "A-9", now=NOW), (int(round((60 * 0.25 * 2 + 200) / 1.5)), 3))

    def test_busy_time_counts_side_by_side_work_once(self):
        d = harness.tmpdir(self, prefix="busy-", dir=TMP)
        p = os.path.join(d, "t.jsonl")
        with open(p, "w") as f:
            for t in (0, 60, 120, 2000, 2060):  # a 30-minute pause between 120 and 2000 is not work
                f.write(json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + t))}) + "\n")
        spans = ds._est_busy(p, "XLI-5")
        self.assertEqual([[b - a for a, b in spans["XLI-5"]]], [[120.0, 60.0]])
        hist = ds._est_spans_history({"a": {"spans": spans}, "b": {"spans": {"XLI-5": [[NOW + 30, NOW + 90]]}}})
        self.assertAlmostEqual(hist["XLI-5"]["minutes"], 3.0)

    def test_like_narrows_and_never_refuses(self):
        hist = {"XLI-1": h(60, 1), "XLI-2": h(180, 1), "XLI-3": h(170, 1)}
        self.assertEqual(ds.est_estimate(hist, "XLI-8", like=["XLI-1"], now=NOW), (60, 1))
        self.assertEqual(ds.est_estimate(hist, "XLI-8", like=["XLI-77"], now=NOW)[1], 3)  # unknown like: all cards
        self.assertEqual(ds.est_estimate({}, "XLI-8", now=NOW), (ds.EST_DEFAULT_MINUTES, 0))

    def test_line_never_shows_the_method(self):
        line = ds.est_line("XLI-8", 150, 6)
        self.assertEqual(line, "XLI-8: about 2.5 h (from 6 of your cards)")
        self.assertNotRegex(ds.est_line("XLI-8", 120, 0), r"median|weight|average")

    def test_in_progress_is_the_last_day(self):
        hist = {"XLI-1": h(60, 0.1), "XLI-2": h(60, 3), "no card": h(9, 0)}
        self.assertEqual(ds.est_in_progress(hist, now=NOW), ["XLI-1"])


class Tagging(unittest.TestCase):
    def write(self, text, desc=""):
        d = harness.tmpdir(self, prefix="sub-", dir=TMP)
        p = os.path.join(d, "agent-a1.jsonl")
        with open(p, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"content": text}}) + "\n")
        return p, {"description": desc}

    def test_first_prompt_card_in_capitals(self):
        p, meta = self.write("You are a builder in ~/x/wt-1023 for plugin-1023, card-417: XLI-417 ... see XLI-417 and XLI-400")
        self.assertEqual(ds.est_subagent_card(p, meta), "XLI-417")
        p, meta = self.write("Build card 1 in wt-1023 (plugin-1023)")
        self.assertIsNone(ds.est_subagent_card(p, meta))
        p, meta = self.write("XLI-413 first line", desc="Review XLI-9 now")
        self.assertEqual(ds.est_subagent_card(p, meta), "XLI-9")  # the description wins


class FakeClient:
    def __init__(self, rows=None):
        self.wb, self.rows, self.writes = "wb", dict(rows or {}), []

    def tool(self, name, args, what, missing_ok=False, soft=False):
        if name == "ledger_rows_get":
            return {"rows": [self.rows[k] for k in args["keys"] if k in self.rows]}
        if name == "ledger_rows_upsert":
            self.writes.append(args["rows"])
            for r in args["rows"]:
                self.rows.setdefault(r["card"], {}).update(r)
            return {"ok": True}
        return {}


class Record(unittest.TestCase):
    def test_written_once_then_only_the_actual(self):
        hist = {"XLI-1": h(60, 1), "XLI-2": h(180, 1), "XLI-3": h(210, 1)}
        c = FakeClient()
        out = ds.est_record(c, hist, ["XLI-9"], now=NOW)
        self.assertEqual(out, [("XLI-9", 150, 3, 0)])
        self.assertEqual(c.writes[0][0]["estimate_minutes"], 150)
        hist["XLI-9"] = h(170, 0)
        hist["XLI-4"] = h(900, 0)  # a new card later never moves the stored estimate
        out = ds.est_record(c, hist, ["XLI-9"], now=NOW)
        self.assertEqual(out, [("XLI-9", 150, 3, 170.0)])
        self.assertEqual(c.writes[1], [{"card": "XLI-9", "actual_minutes": 170}])
        ds.est_record(c, hist, ["XLI-9"], now=NOW)
        self.assertEqual(len(c.writes), 2)  # nothing new: nothing written


class Prompt(unittest.TestCase):
    def setUp(self):
        self.saved = ds._own_agent
        ds._own_agent = lambda inp: {"slug": "a", "name": "a", "workbook_id": "wb", "agent_id": "x"}
        if os.path.exists(ds.EST_TRACKER_PATH):
            os.replace(ds.EST_TRACKER_PATH, os.path.join(TMP, f"old-est-{time.time()}"))

    def tearDown(self):
        ds._own_agent = self.saved

    def ctx(self, prompt):
        return ds.estimate_prompt_context({"prompt": prompt})

    def test_typed_dds_estimate_any_card(self):
        c = self.ctx("DDS estimate XLI-501")
        self.assertIn("`datastash estimate XLI-501`", c)
        self.assertIn("datastash estimate XLI-7", self.ctx("datastash estimate xli-7"))
        self.assertIn("never refuse", c)

    def test_how_long_and_build_list_rules(self):
        self.assertIn("never give your own guess", self.ctx("how long will XLI-12 take?"))
        c = self.ctx("give me the build list for this week")
        self.assertIn("each card's estimate inline", c)
        self.assertIn("done-around", c)
        self.assertIsNone(self.ctx("fix the login bug"))

    def test_tracker_answer_only_while_asked(self):
        self.assertNotIn("answered", self.ctx("yes") or "")
        self.assertIsNone(ds.est_tracker_choice())
        lines = ds.est_tracker_lines([("XLI-9", 150, 3, 0)])
        self.assertIn(ds.EST_TRACKER_ASK, lines[0])
        self.assertIn("answered yes", self.ctx("yes"))
        self.assertEqual(ds.est_tracker_choice(), "yes")
        lines = ds.est_tracker_lines([("XLI-9", 150, 3, 180)])
        self.assertIn('"DDS Development Time Estimate"', lines[0])
        self.assertIn("edited in place", lines[0])
        self.assertIn("Actual: 3.0 h", lines[1])
        self.assertIn("Difference: +30 min", lines[1])


class Guidance(unittest.TestCase):
    def test_checklist_skill_and_whats_new(self):
        items = [i for _h, its in ds.FULL_DDS for i in its]
        self.assertEqual(len([i for i in items if i[0] == "C23"]), 1)
        self.assertIn("1.0.25", ds.WHATS_NEW)
        skill = open(os.path.join(ds.PLUGIN_ROOT, "skills", "datastash-agent", "SKILL.md")).read()
        self.assertIn("datastash estimate <card ids>", skill)
        self.assertIn("DDS Development Time Estimate", skill)


if __name__ == "__main__":
    unittest.main()
