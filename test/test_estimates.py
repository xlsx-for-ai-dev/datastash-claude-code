#!/usr/bin/env python3
"""Offline tests for XLI-426 experience-based time estimates (plugin 1.0.25): `datastash estimate`.
- One best estimate per card from the account's most similar finished runs (shape: pieces, parts, size; newer ones weigh
  more; never the card's own run at work, never "no card", nothing under EST_MIN_CARD_MINUTES); different shapes give
  different estimates; --like narrows to the named cards; no history still answers.
- The look-back splits a card's work into runs and keeps each run's shape and working time.
- The tracker yes/no is the account's (datastash), asked once; the one titled comment is edited in place.
- A subagent's card comes from its description, else the id written most often in capitals in its first prompt.
- est_record writes a card's estimate once and later only refreshes its actual.
- The prompt hook: the person's typed "DDS estimate XLI-12" (any card), the how-long rule, the build-list rule, and the
  one-time tracker yes/no (recorded only while asked and unanswered).
No network, no key; under test/harness.py.
Run: python3 -I test/test_estimates.py"""
import json
import math
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
        self.assertEqual((m, n), (int(round((60 * 180 * 150) ** (1 / 3))), 3))  # XLI-9 itself, "no card", the 3-minute card out

    def test_newer_cards_weigh_more(self):
        hist = {"A-1": h(60, 2), "A-2": h(60, 2), "A-3": h(200, 0)}
        want = math.exp((0.25 * math.log(60) * 2 + math.log(200)) / 1.5)
        self.assertEqual(ds.est_estimate(hist, "A-9", now=NOW), (int(round(want)), 3))

    def test_busy_time_counts_side_by_side_work_once(self):
        d = harness.tmpdir(self, prefix="busy-", dir=TMP)
        p = os.path.join(d, "t.jsonl")
        with open(p, "w") as f:
            for t in (0, 60, 120, 2000, 2060):  # a 30-minute pause between 120 and 2000 is not work
                f.write(json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + t))}) + "\n")
        spans = ds._est_busy(p, "XLI-5")["spans"]
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


def run(minutes, days_ago, pieces, parts="both", lines=500, card_minutes=None):
    end = NOW - days_ago * DAY
    return {"start": end - minutes * 60, "end": end, "own": card_minutes or minutes, "minutes": float(minutes),
            "pieces": pieces, "parts": parts, "lines": lines}


class Similar(unittest.TestCase):
    def hist(self):
        out = {}
        for i in range(25):  # one-piece cards take about 90 minutes, four-piece ones about 3 hours
            out[f"S-{i}"] = {"minutes": 90, "last": NOW - DAY, "runs": [run(85 + i, 0.5 + i * 0.01, 1)]}
            out[f"L-{i}"] = {"minutes": 180, "last": NOW - DAY, "runs": [run(175 + i, 0.5 + i * 0.01, 4)]}
        return out

    def test_different_shapes_rest_on_different_cards(self):
        hist = self.hist()
        one = ds.est_estimate(hist, "XLI-1", now=NOW, shape={"pieces": 1, "parts": "both", "lines": 500})
        four = ds.est_estimate(hist, "XLI-2", now=NOW, shape={"pieces": 4, "parts": "both", "lines": 500})
        self.assertLess(abs(one[0] - 90), 15)
        self.assertLess(abs(four[0] - 180), 15)
        self.assertEqual(one[1], ds.EST_NEAREST)

    def test_parts_and_size_count_too(self):
        hist = {f"P-{i}": {"minutes": 60, "last": NOW, "runs": [run(60, 0.5, 2, "plugin", 100)]} for i in range(25)}
        hist.update({f"V-{i}": {"minutes": 200, "last": NOW, "runs": [run(200, 0.5, 2, "server", 2000)]} for i in range(25)})
        p = ds.est_estimate(hist, "X-1", now=NOW, shape={"pieces": 2, "parts": "plugin", "lines": 100})[0]
        v = ds.est_estimate(hist, "X-2", now=NOW, shape={"pieces": 2, "parts": "server", "lines": 2000})[0]
        self.assertLess(p, 75)
        self.assertGreater(v, 180)

    def test_own_run_at_work_is_left_out_and_gives_the_shape(self):
        hist = self.hist()
        hist["XLI-7"] = {"minutes": 40, "last": NOW - 600, "runs": [run(40, 600 / DAY, 4)]}
        self.assertEqual(ds.est_shape(hist, "XLI-7", now=NOW)["pieces"], 4)
        m, _n = ds.est_estimate(hist, "XLI-7", now=NOW)
        self.assertLess(abs(m - 180), 15)  # rests on the four-piece cards, never on its own 40 minutes so far
        self.assertEqual(ds.est_shape(hist, "XLI-8", now=NOW, pieces=1)["pieces"], 1)


class Runs(unittest.TestCase):
    def test_runs_split_on_a_gap_and_count_everything_at_work_then(self):
        t0 = NOW - 2 * DAY
        files = {
            "main": {"kind": "main", "parts": [], "lines": 0, "spans": {"no card": [[t0, t0 + 3600]]}},
            "sub-a": {"kind": "build", "parts": ["server"], "lines": 300, "spans": {"XLI-5": [[t0 + 600, t0 + 1800]]}},
            "sub-b": {"kind": "build", "parts": ["plugin"], "lines": 100, "spans": {"XLI-5": [[t0 + 2400, t0 + 3000]]}},
            "sub-r": {"kind": "other", "parts": ["plugin", "server"], "lines": 0, "spans": {"XLI-5": [[t0 + 3000, t0 + 3300]]}},
            "later": {"kind": "build", "parts": ["server"], "lines": 50, "spans": {"XLI-5": [[t0 + 9 * 3600, t0 + 10 * 3600]]}},
        }
        hist = ds._est_history(files)
        runs = hist["XLI-5"]["runs"]
        self.assertEqual(len(runs), 2)
        first = runs[0]
        self.assertAlmostEqual(first["minutes"], (3300 - 600) / 60.0)  # the person's own session at work then counts
        self.assertEqual((first["pieces"], first["parts"], first["lines"]), (2, "both", 400))
        self.assertEqual((runs[1]["pieces"], runs[1]["parts"]), (1, "server"))
        self.assertAlmostEqual(ds.est_actual(hist, "XLI-5"), first["minutes"] + 60.0)

    def test_kind_from_the_description(self):
        d = harness.tmpdir(self, prefix="kind-", dir=TMP)
        sub = os.path.join(d, "subagents")
        os.makedirs(sub)
        for name, desc in (("agent-a1", "Build repeats"), ("agent-a2", "Review repeats: #542")):
            open(os.path.join(sub, name + ".jsonl"), "w").close()
            with open(os.path.join(sub, name + ".meta.json"), "w") as f:
                json.dump({"description": desc}, f)
        self.assertEqual(ds._est_kind(os.path.join(sub, "agent-a1.jsonl")), "build")
        self.assertEqual(ds._est_kind(os.path.join(sub, "agent-a2.jsonl")), "other")
        self.assertEqual(ds._est_kind(os.path.join(d, "main.jsonl")), "main")


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
        self.assertEqual(out, [("XLI-9", 131, 3, 0)])  # (60 * 180 * 210) ** (1/3)
        self.assertEqual(c.writes[0][0]["estimate_minutes"], 131)
        hist["XLI-9"] = h(170, 0)
        hist["XLI-4"] = h(900, 0)  # a new card later never moves the stored estimate
        out = ds.est_record(c, hist, ["XLI-9"], now=NOW)
        self.assertEqual(out, [("XLI-9", 131, 3, 170.0)])
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

    def test_tracker_answer_is_the_accounts_and_asked_once(self):
        server = {"answer": None, "asked_at": None}
        calls = []

        def fake(method, path, key, body=None, timeout=40):
            calls.append((method, body))
            self.assertEqual(path, ds.EST_CHOICE_PATH)
            if body == {"ask": True}:
                first = server["asked_at"] is None and server["answer"] is None
                server["asked_at"] = server["asked_at"] or "t"
                return {"ask": first, **server}
            if body and "answer" in body:
                server["answer"] = body["answer"]
            return dict(server)
        saved, saved_key = ds.api_json, ds._est_choice_key
        ds.api_json, ds._est_choice_key = fake, (lambda: "k")
        try:
            self.assertNotIn("answered", self.ctx("yes") or "")  # not asked yet: a plain yes is not an answer
            self.assertIsNone(server["answer"])
            lines = ds.est_tracker_lines([("XLI-9", 150, 3, 0)], "k")
            self.assertIn(ds.EST_TRACKER_ASK, lines[0])
            self.assertEqual(ds.est_tracker_lines([("XLI-9", 150, 3, 0)], "k"), [])  # asked once for the account, never again
            self.assertIn("answered yes", self.ctx("yes"))
            self.assertEqual(server["answer"], "yes")  # kept on datastash for the whole account
            wrote = []
            lines = ds.est_tracker_lines([("XLI-9", 150, 3, 180), ("XLI-10", 60, 3, 0)], "k",
                                         write=lambda c, b, m, a: wrote.append((c, b)) or ("Linear" if c == "XLI-9" else None))
            self.assertEqual(wrote[0][0], "XLI-9")
            self.assertIn("Actual: 3.0 h", wrote[0][1])
            self.assertIn("Difference: +30 min", wrote[0][1])
            self.assertIn('"DDS Development Time Estimate" kept on Linear', lines[0])
            self.assertIn("edited in place", lines[1])
            self.assertTrue(lines[2].startswith("XLI-10: "))
            server["answer"] = "no"
            self.assertEqual(ds.est_tracker_lines([("XLI-9", 150, 3, 0)], "k"), [])
        finally:
            ds.api_json, ds._est_choice_key = saved, saved_key


class TrackerComment(unittest.TestCase):
    def test_one_titled_comment_edited_in_place(self):
        body = ds.est_tracker_body(150, 6)
        ups, adds = [], []
        up = lambda cid, b: ups.append((cid, b))  # noqa: E731
        add = lambda b: adds.append(b)  # noqa: E731
        self.assertEqual(ds.trk_upsert([("1", "looks good")], up, add, body), "added")
        self.assertEqual(ds.trk_upsert([("1", "x"), ("2", body)], up, add, body), "same")
        newer = ds.est_tracker_body(150, 6, 170)
        self.assertEqual(ds.trk_upsert([("1", "x"), ("2", body)], up, add, newer), "updated")
        self.assertEqual((ups, len(adds)), ([("2", newer)], 1))

    def test_no_login_here_leaves_it_to_the_agent(self):
        self.assertIsNone(ds.trk_comment("XLI-9", ds.est_tracker_body(150, 6)))  # offline tests: no tracker reachable
        self.assertEqual(ds.trk_lists(), [])


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
