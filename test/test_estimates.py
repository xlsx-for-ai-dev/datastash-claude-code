#!/usr/bin/env python3
"""Offline tests for XLI-426 experience-based time estimates (plugin 1.0.25): `datastash estimate`.
- One best estimate per card from the account's most similar finished runs (shape: pieces, parts, size; newer ones weigh
  more; never the card's own run at work, never "no card", nothing under EST_MIN_CARD_MINUTES); different shapes give
  different estimates; --like narrows to the named cards; no history still answers.
- The look-back splits a card's work into runs and keeps each run's shape and working time.
- Estimates on the person's cards: only their typed **DDS estimates on** / **off**, checked and signed as typed datastash
  sync and sent on this computer's login (never a bare yes/no); an unanswered ask at most once per session; no tracker
  write until it is on. The one titled comment is found across every page and edited in place.
- Time per card keeps v1.0.24's card order (description, DATASTASH_CARD, git branch); a subagent's first-prompt card is
  only the last fallback. The look-back skips folders left off and folders with time per card off.
- The actual is the card's Time per card minutes.
- est_record writes a card's estimate once and later only refreshes its actual.
- The prompt hook: the person's typed "DDS estimate XLI-12" (any card), the how-long rule, the build-list rule, and the
  typed DDS estimates on / off handed to the checker.
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
        self.assertAlmostEqual(ds.est_actual(hist, "XLI-5"), (1200 + 900 + 3600) / 60.0)  # its own working time, never wall

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
    def write(self, text, desc="", branch=None):
        d = os.path.join(harness.tmpdir(self, prefix="sub-", dir=TMP), "sid", "subagents")
        os.makedirs(d)
        p = os.path.join(d, "agent-a1.jsonl")
        with open(p, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"content": text}}) + "\n")
            for t in (0, 60, 120):
                f.write(json.dumps({"type": "assistant", "uuid": f"u{t}", "message": {"id": f"m{t}", "model": "m", "content": []},
                                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + t)),
                                    **({"gitBranch": branch} if branch else {})}) + "\n")
        return p, {"description": desc}

    def test_first_prompt_card_in_capitals(self):
        p, _m = self.write("You are a builder in ~/x/wt-1023 for plugin-1023, card-417: XLI-417 ... see XLI-417 and XLI-400")
        self.assertEqual(ds.est_prompt_card(p), "XLI-417")
        p, _m = self.write("Build card 1 in wt-1023 (plugin-1023)")
        self.assertIsNone(ds.est_prompt_card(p))

    def test_v1024_order_kept_prompt_only_a_fallback(self):
        """Review 4: DATASTASH_CARD, then the git branch, beat the prompt's card, as in v1.0.24 (description first)."""
        old = os.environ.pop("DATASTASH_CARD", None)
        try:
            p, _m = self.write("Build XLI-523 XLI-523 XLI-523 (also XLI-426)", branch="xli-426-x")
            rd = ds.TpcReader({}, None, False, ds.est_prompt_card(p))
            self.assertEqual(rd.card_of({"gitBranch": "xli-426-x"}), "XLI-426")  # the branch, not the prompt's XLI-523
            self.assertEqual(rd.card_of({}), "XLI-523")  # nothing else names one: the prompt's card
            self.assertEqual(ds.TpcReader({}, "XLI-9", False, "XLI-523").card_of({"gitBranch": "xli-426-x"}), "XLI-9")
            os.environ["DATASTASH_CARD"] = "XLI-77"
            self.assertEqual(rd.card_of({"gitBranch": "xli-426-x"}), "XLI-77")
            self.assertEqual(set(ds._est_busy(p, None)["spans"]), {"XLI-77"})
            del os.environ["DATASTASH_CARD"]
            self.assertEqual(set(ds._est_busy(p, None)["spans"]), {"XLI-426"})  # the look-back: the same order
            # tpc_read gives a subagent's reader the prompt's card as its fallback only (read once, kept in its state).
            fst = {"offset": 0}
            rd, _off = ds.tpc_read(p, fst, None, False)
            self.assertEqual(fst.get("prompt_card"), "XLI-523")
            self.assertEqual({k[0] for k in rd.rows}, {"XLI-426"})  # its time books to the branch's card, as in v1.0.24
            # tpc_files gives the description's card only, as in v1.0.24 (never the prompt's).
            self.assertNotIn("est_subagent_card", dir(ds))
        finally:
            os.environ.pop("DATASTASH_CARD", None)
            if old is not None:
                os.environ["DATASTASH_CARD"] = old


class Folders(unittest.TestCase):
    """Review 3: the look-back and datastash estimate never use (or send) a folder left off or with time per card off."""

    def test_left_off_and_tpc_off_folders_are_skipped(self):
        saved = ds.is_off, ds.tpc_choice
        ds.is_off = lambda f: f.startswith("/w/off")
        ds.tpc_choice = lambda f: "off" if f == "/w/notpc" else None
        span = {"XLI-1": [[NOW, NOW + 3600]]}
        files = {"/p/a.jsonl": {"cwds": ["/w/on"], "spans": span},
                 "/p/b.jsonl": {"cwds": ["/w/off/sub"], "spans": {"XLI-2": [[NOW, NOW + 3600]]}},
                 "/p/b/subagents/agent-x.jsonl": {"cwds": ["/w/on"], "spans": {"XLI-3": [[NOW, NOW + 3600]]}},
                 "/p/c.jsonl": {"cwds": ["/w/on", "/w/notpc"], "spans": {"XLI-4": [[NOW, NOW + 3600]]}},
                 "/p/d.jsonl": {"cwds": [], "spans": {"XLI-5": [[NOW, NOW + 3600]]}}}
        try:
            self.assertEqual(sorted(ds._est_usable(files)), ["/p/a.jsonl"])
            hist = ds._est_history(ds._est_usable(files))
            self.assertEqual(sorted(hist), ["XLI-1"])
            self.assertEqual(ds.est_in_progress(hist, NOW + 3600), ["XLI-1"])
        finally:
            ds.is_off, ds.tpc_choice = saved

    def test_busy_keeps_the_folders(self):
        d = harness.tmpdir(self, prefix="cwd-", dir=TMP)
        p = os.path.join(d, "t.jsonl")
        with open(p, "w") as f:
            for t, c in ((0, "/w/on"), (60, "/w/on/x")):
                f.write(json.dumps({"cwd": c, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + t))}) + "\n")
        self.assertEqual(ds._est_busy(p, "XLI-5")["cwds"], ["/w/on", "/w/on/x"])


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

    def test_actual_is_the_cards_time_per_card_minutes(self):
        """Review 7: the actual comes from Time per card (its minutes), never the look-back's wall time."""
        hist = {"XLI-1": h(60, 1), "XLI-9": {"minutes": 50.0, "last": NOW, "runs": [{"minutes": 400.0, "own": 50.0, "end": NOW}]}}
        c = FakeClient({"XLI-9": {"card": "XLI-9", "estimate_minutes": 90, "rests_on": 2}})
        out = ds.est_record(c, hist, ["XLI-9"], now=NOW, actuals={"XLI-9": 72.4})
        self.assertEqual(out, [("XLI-9", 90, 2, 72.4)])
        self.assertEqual(c.writes[-1], [{"card": "XLI-9", "actual_minutes": 72}])
        self.assertEqual(ds.est_actual(hist, "XLI-9"), 50.0)  # no workbook: its own working time, never 400 wall


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

    def test_bare_yes_never_answers_only_the_typed_phrase(self):
        """Review 1: a bare yes/ok answers nothing; DDS estimates on / off goes to the checker, nothing is saved here."""
        started = []
        saved = ds.hook_shape, ds.start_check, ds.api_json
        ds.hook_shape = lambda inp: ("sid", "/t.jsonl", "prompt-abcdefgh")
        ds.start_check = lambda shape, kind, prompt, data: started.append((kind, prompt, data)) or True
        ds.api_json = lambda *a, **k: self.fail("no datastash call from the prompt hook")
        try:
            for said in ("yes", "ok", "y", "sure", "no", "yes please"):
                self.assertNotIn("datastash estimates: the plugin", self.ctx(said) or "")
            self.assertEqual(started, [])
            self.assertIn(ds.EST_ON_SAYING, self.ctx("DDS estimates on"))
            self.assertIn(ds.EST_OFF_SAYING, self.ctx("datastash estimates off."))
            self.assertIsNone(ds.est_onoff_kind("please turn DDS estimates on"))  # only the whole short prompt
            self.assertEqual([k for k, _p, _d in started], ["estimates_on", "estimates_off"])
            self.assertEqual(started[0][2], {"agent_id": "x"})
        finally:
            ds.hook_shape, ds.start_check, ds.api_json = saved

    def test_checker_signs_the_typed_sync_proof_and_sends_it_on_the_login(self):
        import hashlib
        import hmac
        sent = []
        aid, grant = "11111111-2222-4333-8444-555555555555", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        saved = ds.read_answer_key, ds.login_token, ds.http
        ds.read_answer_key = lambda: ("answer-key", grant)
        ds.login_token = lambda force=False: "dsat_token"
        ds.http = lambda method, path, key=None, body=None, timeout=40, **k: sent.append((method, path, key, body)) or (200, "", json.dumps({"answer": body["answer"]}))
        try:
            typed = {"pid": "prompt-abcdefgh", "prompt": "DDS estimates on", "typed_at": int(time.time() * 1000) - 1000}
            rec = {"kind": "estimates_on", "prompt": "DDS estimates on", "data": {"agent_id": aid}}
            self.assertEqual(ds.send_estimates_choice(rec, typed), [ds.EST_ON_DONE])
            method, path, key, body = sent[0]
            self.assertEqual((method, path, key, body["answer"]), ("POST", ds.EST_CHOICE_PATH, "dsat_token", "yes"))
            pr = body["typed_proof"]
            self.assertEqual(sorted(pr), ["agent_id", "iat", "mac", "prompt_id", "typed_at", "typed_sha256", "v"])
            lines = [ds.TYPED_SYNC_LABEL, f"grant:{grant}", aid, "estimates_on", pr["prompt_id"],
                     hashlib.sha256(b"DDS estimates on").hexdigest(), str(pr["typed_at"]), str(pr["iat"])]
            self.assertEqual(pr["mac"], hmac.new(b"answer-key", "\n".join(lines).encode(), hashlib.sha256).hexdigest())
            # A record whose prompt is not that phrase, or no login here: nothing sent.
            self.assertEqual(ds.send_estimates_choice(dict(rec, prompt="yes"), typed), [])
            ds.read_answer_key = lambda: None
            self.assertEqual(ds.send_estimates_choice(rec, typed), [ds.EST_ONOFF_NO_LOGIN])
            self.assertEqual(len(sent), 1)
        finally:
            ds.read_answer_key, ds.login_token, ds.http = saved

    def test_ask_once_per_session_again_later_and_no_writes_until_on(self):
        server = {"answer": None, "asked_at": None}

        def fake(method, path, key, body=None, timeout=40):
            self.assertEqual(path, ds.EST_CHOICE_PATH)
            if body == {"ask": True}:
                return {"ask": server["answer"] is None, **server}
            self.assertIsNone(body)  # an agent never sends the answer
            return dict(server)
        saved = ds.api_json, ds.lazy_me
        sid = ["sess-%d" % time.time_ns()]
        ds.api_json, ds.lazy_me = fake, (lambda inp=None: (lambda only: sid[0]))
        wrote = []
        w = lambda c, b, m, a: wrote.append(c) or "Linear"  # noqa: E731
        try:
            lines = ds.est_tracker_lines([("XLI-9", 150, 3, 0)], "k", write=w)
            self.assertIn(ds.EST_TRACKER_ASK, lines[0])
            self.assertIn("Type **DDS estimates on**", lines[0])
            self.assertEqual(ds.est_tracker_lines([("XLI-9", 150, 3, 0)], "k", write=w), [])  # once in this session
            sid[0] += "-next"
            self.assertIn(ds.EST_TRACKER_ASK, ds.est_tracker_lines([("XLI-9", 150, 3, 0)], "k", write=w)[0])  # asked again
            self.assertEqual(wrote, [])  # not on: nothing written on any card
            server["answer"] = "yes"
            lines = ds.est_tracker_lines([("XLI-9", 150, 3, 180), ("XLI-10", 60, 3, 0)], "k",
                                         write=lambda c, b, m, a: wrote.append((c, b)) or ("Linear" if c == "XLI-9" else None))
            self.assertEqual(wrote[0][0], "XLI-9")
            self.assertIn("Actual: 3.0 h", wrote[0][1])
            self.assertIn("Difference: +30 min", wrote[0][1])
            self.assertIn('"DDS Development Time Estimate" kept on Linear', lines[0])
            self.assertIn("edited in place", lines[1])
            self.assertTrue(lines[2].startswith("XLI-10: "))
            server["answer"] = "no"
            self.assertEqual(ds.est_tracker_lines([("XLI-9", 150, 3, 0)], "k", write=w), [])
            ds.api_json = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
            self.assertEqual(ds.est_tracker_lines([("XLI-9", 150, 3, 0)], "k", write=w), [])  # unreachable: nothing
        finally:
            ds.api_json, ds.lazy_me = saved


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

    def test_finds_the_comment_on_any_page(self):
        """Review 6: the estimate comment on a later page is edited, never a second one added (GitHub, Linear, Jira)."""
        body = ds.est_tracker_body(150, 6, 170)
        old = ds.est_tracker_body(150, 6)
        calls = []
        many = [{"id": i, "body": f"note {i}"} for i in range(150)] + [{"id": 999, "body": old}]

        def gh(args, b=None):
            calls.append(("gh", args, b))
            if args[0].startswith("repos/o/r/issues/12/comments?"):
                page = int(args[0].rsplit("page=", 1)[1])
                return many[(page - 1) * 100:page * 100]
            return {}

        def linear(query, v):
            calls.append(("linear", v))
            if "comments" in query:
                nodes = [{"id": str(i), "body": f"n {i}"} for i in range(100)] if v["after"] is None else [{"id": "L9", "body": old}]
                return {"issue": {"id": "I1", "comments": {"nodes": nodes, "pageInfo": {"hasNextPage": v["after"] is None, "endCursor": "c1"}}}}
            return {}

        def jira(method, path, b=None):
            calls.append(("jira", method, path, b))
            if method == "GET":
                start = int(path.split("startAt=")[1].split("&")[0])
                allc = [{"id": str(i), "body": "x"} for i in range(100)] + [{"id": "J9", "body": old}]
                return {"comments": allc[start:start + 100], "total": len(allc)}
            return {}
        saved = ds._trk_gh, ds._trk_linear, ds._trk_jira
        ds._trk_gh, ds._trk_linear, ds._trk_jira = gh, linear, jira
        try:
            self.assertEqual(ds.trk_comment("o/r#12", body), "GitHub")
            self.assertIn(("gh", ["-X", "PATCH", "repos/o/r/issues/comments/999"], {"body": body}), calls)
            self.assertFalse(any(c[0] == "gh" and "POST" in c[1] for c in calls))
            self.assertEqual(ds.trk_comment("XLI-9", body), "Linear")
            self.assertTrue(any(c[0] == "linear" and c[1].get("id") == "L9" for c in calls))
            self.assertFalse(any(c[0] == "linear" and "i" in c[1] for c in calls))  # no commentCreate
            ds._trk_linear = lambda q, v: {}  # not a Linear card: Jira
            self.assertEqual(ds.trk_comment("PROJ-7", body), "Jira")
            self.assertIn(("jira", "PUT", "/rest/api/2/issue/PROJ-7/comment/J9", {"body": body}), calls)
            self.assertFalse(any(c[0] == "jira" and c[1] == "POST" for c in calls))
        finally:
            ds._trk_gh, ds._trk_linear, ds._trk_jira = saved

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
