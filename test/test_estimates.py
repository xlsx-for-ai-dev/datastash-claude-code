#!/usr/bin/env python3
"""Offline tests for XLI-426 experience-based time estimates (plugin 1.0.25): `datastash estimate`.
One focused test per must-have of the rebuild:
- Card writing turns on only by the person's whole typed "DDS estimates on" (off: "DDS estimates off"), proved typed and
  signed as typed sync, sent on this computer's login; never a bare yes/ok; the unanswered ask once a session.
- The estimates prompt hook runs after every other prompt hook: a typed answer "N: ..." still goes out, the welcome-back
  list still shows; the estimates note is only added.
- Never a write to a tab that is not ours: only the estimates_tab burn setup names; none: nothing is written.
- The actual is the card's Time per card minutes, kept only once the card is done; the comment shows Actual and
  Difference only then.
- The look-back skips folders left off and folders with time per card off (and their subagents).
- Card attribution: time per card's reader is unchanged (DATASTASH_CARD, then the git branch); the look-back uses the
  description, the git branch, then the subagent's first-prompt card, and never today's DATASTASH_CARD on past work.
- The tracker comment is found across every page and edited in place; never a second one.
- The estimate: one number from the account's own finished stages (newer weigh more), never the card's own, never refused.
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
import harness  # noqa: E402
from test_status import Base, ds, WB  # noqa: E402
from test_show_asks_answer import AsksBase, KEY, GRANT  # noqa: E402

NOW = 1_800_000_000.0
DAY = 86400.0
AID = "11111111-aaaa-bbbb-cccc-0000000004a6"


def stage(minutes, days_ago):
    end = NOW - days_ago * DAY
    return {"minutes": float(minutes), "start": end - minutes * 60, "end": end}


class Estimate(unittest.TestCase):
    def test_one_number_from_other_finished_stages_newer_weigh_more(self):
        hist = {"A-1": {"stages": [stage(60, 1)]}, "A-2": {"stages": [stage(240, 4)]}, "A-3": {"stages": [stage(5, 1)]},
                "no card": {"stages": [stage(5000, 1)]},
                "A-4": {"stages": [stage(90, 0.1)]}}  # A-4 still going (2.4 h ago): not used yet
        m, n = ds.est_estimate(hist, "A-9", now=NOW)
        w1, w2 = 0.5 ** (1 / 3), 0.5 ** (4 / 3)
        self.assertEqual((m, n), (int(round(math.exp((w1 * math.log(60) + w2 * math.log(240)) / (w1 + w2)))), 2))
        self.assertEqual(ds.est_estimate({}, "A-1", now=NOW), (ds.EST_DEFAULT_MINUTES, 0))  # never refuses

    def test_different_history_gives_a_different_estimate_and_lines_never_show_the_method(self):
        a = ds.est_estimate({"A-1": {"stages": [stage(60, 1)]}}, "X-1", now=NOW)
        b = ds.est_estimate({"A-1": {"stages": [stage(60, 1)]}, "A-2": {"stages": [stage(300, 0.5)]}}, "X-1", now=NOW)
        self.assertNotEqual(a, b)
        self.assertEqual(ds.est_line("X-1", 150, 6), "X-1: about 2.5 h (from 6 of your cards)")
        self.assertNotRegex(ds.est_line("X-1", 150, 0) + ds.est_line("X-1", 150, 6), r"weight|average|median|log")

    def test_the_cards_shape_and_its_own_earlier_stages(self):
        hist = {f"S-{i}": {"stages": [dict(stage(40, 1), parts="server")]} for i in range(3)}
        hist.update({f"B-{i}": {"stages": [dict(stage(160, 1), parts="both")]} for i in range(3)})
        self.assertEqual(ds.est_estimate(hist, "N-1", now=NOW, parts="both"), (160, 3))
        self.assertEqual(ds.est_estimate(hist, "N-1", now=NOW, parts="server"), (40, 3))
        self.assertEqual(ds.est_estimate(hist, "N-1", now=NOW, parts="plugin")[1], 6)  # too few of that shape: all of them
        hist["N-1"] = {"stages": [dict(stage(400, 1), parts="both"), dict(stage(10, 0.05), parts="both")]}
        m, n = ds.est_estimate(hist, "N-1", now=NOW)  # its stage at work gives the shape; its earlier stage counts more
        self.assertEqual(n, 4)
        self.assertEqual(m, int(round(math.exp((3 * math.log(160) + 3 * math.log(400)) / 6))))
        hist2 = {"N-2": {"stages": [dict(stage(30, 0.01), parts="server")]}}
        self.assertEqual(ds.est_shape(hist2, "N-2", now=NOW), "server")

    def test_stages_split_on_a_gap(self):
        h = 3600.0
        st = ds.est_stages([[30, NOW, NOW + h], [20, NOW + 2 * h, NOW + 3 * h], [40, NOW + 20 * h, NOW + 21 * h]])
        self.assertEqual([s["minutes"] for s in st], [50.0, 40.0])

    def test_account_cards_count_once(self):
        spans = {"A-1": [[45, NOW - 4 * DAY, NOW - 4 * DAY + 3600]]}
        acct = {"A-1": {"minutes": 999, "last": NOW}, "B-2": {"minutes": 80, "first": NOW - DAY, "last": NOW - DAY + 60}}
        hist = ds.est_history(spans, acct)
        self.assertEqual(hist["A-1"]["minutes"], 45)  # the look-back's own stages, never added twice
        self.assertEqual(hist["B-2"]["stages"][0]["minutes"], 80)


class Attribution(unittest.TestCase):
    def write(self, path, entries):
        with open(path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_time_per_card_order_unchanged_and_lookback_never_uses_todays_env(self):
        os.environ["DATASTASH_CARD"] = "ENV-1"
        try:
            rd = ds.TpcReader({})
            self.assertEqual(rd.card_of({"gitBranch": "xli-7-x"}), "ENV-1")  # v1.0.24: DATASTASH_CARD before the branch
            self.assertEqual(ds.TpcReader({}, "DESC-3").card_of({"gitBranch": "xli-7-x"}), "DESC-3")
            est = ds._EstReader({}, None, "XLI-9")
            self.assertEqual(est.card_of({"gitBranch": "xli-7-x"}), "XLI-7")  # branch before the prompt card
            self.assertEqual(est.card_of({"gitBranch": "HEAD"}), "XLI-9")  # the prompt card last
            self.assertEqual(ds._EstReader({}, "DESC-3", "XLI-9").card_of({"gitBranch": "xli-7-x"}), "DESC-3")
        finally:
            os.environ.pop("DATASTASH_CARD", None)

    def test_prompt_card_is_an_id_as_written(self):
        d = harness.tmpdir(self, prefix="pc-", dir=harness.CONF)
        p = os.path.join(d, "a.jsonl")
        self.write(p, [{"type": "user", "message": {"content": "Build XLI-413 in ~/x/wt-1023 (plugin-1022, PR-5) for XLI-413; see XLS-1."}}])
        self.assertEqual(ds.est_prompt_card(p), "XLI-413")
        self.write(p, [{"type": "user", "message": {"content": "Build card 1 in wt-1023 on branch plugin-1023"}}])
        self.assertIsNone(ds.est_prompt_card(p))


class LeftOff(Base):
    def test_folders_left_off_or_time_per_card_off_are_skipped_with_their_subagents(self):
        on = os.path.realpath(harness.tmpdir(self, prefix="on-", dir=harness.CONF))
        off = os.path.realpath(harness.tmpdir(self, prefix="off-", dir=harness.CONF))
        tpc_off = os.path.realpath(harness.tmpdir(self, prefix="tpcoff-", dir=harness.CONF))
        ds.record_left_off([off], "web-app")
        ds._tpc_update(lambda s: s["projects"].__setitem__(tpc_off, {"on": False, "at": 1}))
        card = {"A-1": [50, NOW - DAY, NOW - DAY + 60]}
        files = {
            "/p/on.jsonl": {"cwds": [on], "cards": card},
            "/p/on/sub.jsonl": {"cwds": [on], "cards": {"A-2": [50, NOW - DAY, NOW - DAY + 60]}, "sub": True, "main": "/p/on.jsonl"},
            "/p/off.jsonl": {"cwds": [off], "cards": {"B-1": [50, NOW - DAY, NOW - DAY + 60]}},
            "/p/off/sub.jsonl": {"cwds": [on], "cards": {"B-2": [50, NOW - DAY, NOW - DAY + 60]}, "sub": True, "main": "/p/off.jsonl"},
            "/p/mixed.jsonl": {"cwds": [on, tpc_off], "cards": {"C-1": [50, NOW - DAY, NOW - DAY + 60]}},
            "/p/nofolder.jsonl": {"cwds": [], "cards": {"D-1": [50, NOW - DAY, NOW - DAY + 60]}},
        }
        self.assertEqual(sorted(ds.est_spans(files)), ["A-1", "A-2"])


class FakeClient:
    def __init__(self, rows=None, refuse_writes=False):
        self.wb, self.key, self.cards_tab = WB, "ldgr_ag_test", "Time per card"
        self.rows, self.calls = rows or {}, []

    def tool(self, name, args, what, missing_ok=False, soft=False):
        self.calls.append((name, json.loads(json.dumps(args))))
        if name == "ledger_rows_get":
            return {"rows": [dict(self.rows[k], card=k) for k in args["keys"] if k in self.rows]}
        if name == "ledger_rows_upsert":
            for r in args["rows"]:
                self.rows.setdefault(r["card"], {}).update({k: v for k, v in r.items() if k != "card"})
            return {"ok": True}
        return {}

    def filter(self, tab, where, order=None, pages=None):
        return []

    def writes(self):
        return [a for n, a in self.calls if n == "ledger_rows_upsert"]


class TabHandling(Base):
    def setUp(self):
        super().setUp()
        self.saved_t = ds._tpc_setup
        self.addCleanup(lambda: setattr(ds, "_tpc_setup", self.saved_t))
        self.agent = {"workbook_id": WB}

    def test_a_workbook_whose_estimates_tab_is_not_ours_gets_no_write(self):
        ds._tpc_setup = lambda agent, key: ({"ok": True, "card_tab": "Time per card", "estimates_tab": None}, None)
        self.assertIsNone(ds.est_tab(self.agent, "k"))
        c = FakeClient()
        rows = ds.est_record(c, None, {"A-1": {"stages": [stage(60, 1)]}}, ["X-1"], now=NOW)
        self.assertEqual(rows[0][0], "X-1")
        self.assertEqual(c.calls, [])  # nothing read or written, the estimate still answered
        ds.est_after_send(c, [{"card": "X-1"}], self.agent, NOW)
        self.assertEqual(c.writes(), [])

    def test_the_tab_burn_setup_names_is_the_only_one_written(self):
        ds._tpc_setup = lambda agent, key: ({"ok": True, "estimates_tab": "Estimates"}, None)
        self.assertEqual(ds.est_tab(self.agent, "k"), "Estimates")
        ds._tpc_setup = lambda agent, key: self.fail("asked again within a day")
        self.assertEqual(ds.est_tab(self.agent, "k"), "Estimates")
        c = FakeClient()
        ds.est_record(c, "Estimates", {"A-1": {"stages": [stage(60, 1)]}}, ["X-1"], now=NOW)
        [w] = c.writes()
        self.assertEqual((w["tab"], w["key"], sorted(w["rows"][0])), ("Estimates", "card", ["card", "estimate_minutes", "estimated_at", "rests_on"]))


class Actual(Base):
    def test_actual_only_once_done_from_time_per_card_minutes(self):
        c = FakeClient()
        hist = {"A-1": {"stages": [stage(60, 1)]}, "X-1": {"stages": [stage(30, 0)], "minutes": 30}}
        [(card, m, n, actual)] = ds.est_record(c, "Estimates", hist, ["X-1"], now=NOW, actuals={"X-1": 95})
        self.assertIsNone(actual)  # in progress: no actual, no difference
        self.assertNotIn("actual_minutes", c.rows["X-1"])
        self.assertNotIn("Actual", ds.est_comment_body(m, n, actual))
        self.assertNotIn("done in", ds.est_line(card, m, n, actual))
        [(_c, m2, _n, actual)] = ds.est_record(c, "Estimates", hist, ["X-1"], now=NOW + 60, done={"X-1": NOW}, actuals={"X-1": 95})
        self.assertEqual((m2, actual), (m, 95))  # the estimate is the stored one; the actual is Time per card's 95
        self.assertEqual((c.rows["X-1"]["actual_minutes"], c.rows["X-1"]["estimate_minutes"]), (95, m))
        body = ds.est_comment_body(m, n, actual)
        self.assertTrue(body.startswith(ds.EST_COMMENT_TITLE))
        self.assertIn("Actual: 1.6 h", body)
        self.assertIn("Difference: ", body)


class Comment(unittest.TestCase):
    def test_found_on_a_later_page_and_edited_in_place(self):
        pages = [[{"id": i, "body": f"note {i}"} for i in range(100)], [{"id": 500, "body": ds.EST_COMMENT_TITLE + "\n\nEstimate: 1 h"}]]
        done = []

        def gh(args, body=None):
            a = " ".join(args)
            if "comments?per_page=100&page=" in a:
                return pages[int(a.rsplit("page=", 1)[1]) - 1] if int(a.rsplit("page=", 1)[1]) <= len(pages) else []
            if "-X" in args:
                done.append((args[1], args[2], body))
                return {}
            return {"state": "open"}
        saved = ds._trk_gh
        ds._trk_gh = gh
        try:
            self.assertEqual(ds.trk_comment("o/r#7", 90, 4), "GitHub")
            self.assertEqual(ds.trk_comment("o/r#7", 90, 4), "GitHub")
        finally:
            ds._trk_gh = saved
        self.assertEqual([(m, p) for m, p, _b in done], [("PATCH", "repos/o/r/issues/comments/500")] * 2)
        self.assertFalse(any(m == "POST" for m, _p, _b in done))
        self.assertIn("Estimate: 1.5 h (from 4 of your cards)", done[0][2]["body"])

    def test_two_at_once_leave_one_comment(self):
        """Two agents here estimate the same card at once, each after its own (stale) read: the card keeps one comment."""
        import threading
        kept, lock = [], threading.Lock()
        start = threading.Barrier(2)

        def gh(args, body=None):
            a = " ".join(args)
            if "comments?per_page=100&page=" in a:
                with lock:
                    now = [dict(c) for c in kept]
                time.sleep(0.2)  # a slow tracker: the other agent runs meanwhile
                return now if a.endswith("page=1") else []
            if "-X" in args and args[1] == "POST":
                with lock:
                    kept.append({"id": len(kept) + 1, "body": body["body"]})
                return {}
            if "-X" in args:
                return {}
            return {"state": "open"}

        def one():
            ds.trk_card("o/r#7")  # cmd_estimate's read before the lock: both see no comment
            start.wait()
            ds.trk_comment("o/r#7", 90, 4)
        saved = ds._trk_gh
        ds._trk_gh = gh
        try:
            ts = [threading.Thread(target=one) for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(10)
        finally:
            ds._trk_gh = saved
        self.assertEqual(len([c for c in kept if c["body"].startswith(ds.EST_COMMENT_TITLE)]), 1)

    def test_none_there_adds_exactly_one(self):
        made = []
        self.assertEqual(ds.trk_upsert([(1, "x")], lambda i, b: made.append(("edit", i)), lambda b: made.append("add"), "B"), "added")
        self.assertEqual(ds.trk_upsert([(1, ds.EST_COMMENT_TITLE + " old"), (2, ds.EST_COMMENT_TITLE)],
                                       lambda i, b: made.append(("edit", i)), lambda b: made.append("add"), ds.EST_COMMENT_TITLE + " new"), "updated")
        self.assertEqual(made, ["add", ("edit", 1)])


class OnOff(AsksBase):
    def setUp(self):
        super().setUp()
        ds.write_private(ds.conf_path("web-app"), json.dumps({"name": "web-app", "agent_id": AID, "workbook_id": WB,
                                                              "projects": [self.folder]}))
        base = ds.http
        self.est_posts = []

        def http(method, path, key=None, body=None, form=None, timeout=40):
            if path == ds.EST_PATH:
                self.est_posts.append((method, key, body))
                return 200, "application/json", json.dumps({"estimates_on_cards": body["on"]})
            return base(method, path, key, body, form, timeout)
        ds.http = http

    def test_only_the_whole_typed_phrase_turns_it_on_signed_on_the_login(self):
        for text in ("yes", "ok", "y", "DDS estimates on please", "turn on DDS estimates on", "2: DDS estimates on"):
            self.assertIsNone(ds.est_onoff_kind(text), text)
        self.assertEqual(ds.est_onoff_kind("DDS estimates on"), "estimates_on")
        self.assertEqual(ds.est_onoff_kind("datastash estimates OFF."), "estimates_off")
        note = self.prompt("DDS estimates on")
        self.assertIn(ds.EST_ON_SAYING, note)
        [(method, key, body)] = self.est_posts
        self.assertEqual((method, key, body["on"]), ("POST", "dsat_person", True))
        p = body["typed_proof"]
        self.assertEqual(p["agent_id"], AID)
        want = ds.typed_sync_proof(KEY, f"grant:{GRANT.lower()}", AID, "estimates_on", p["prompt_id"], "DDS estimates on",
                                   p["typed_at"], p["iat"])
        self.assertEqual(p["mac"], want["mac"])
        self.assertEqual(self.outcomes(), [ds.EST_ON_DONE])
        self.assertIn(ds.EST_OFF_SAYING, self.prompt("datastash estimates off"))
        self.assertEqual([b["on"] for _m, _k, b in self.est_posts], [True, False])
        self.assertEqual(self.est_posts[1][2]["typed_proof"]["mac"], ds.typed_sync_proof(
            KEY, f"grant:{GRANT.lower()}", AID, "estimates_off", self.est_posts[1][2]["typed_proof"]["prompt_id"],
            "datastash estimates off", self.est_posts[1][2]["typed_proof"]["typed_at"], self.est_posts[1][2]["typed_proof"]["iat"])["mac"])

    def test_an_ai_cannot_turn_it_on_and_a_bare_yes_never_does(self):
        self.forged({"session_id": "sess1", "prompt": "DDS estimates on", "hook_event_name": "UserPromptSubmit",
                     "cwd": self.folder, "prompt_id": "forged-prompt-1", "transcript_path": self.transcript("sess1")})
        self.prompt("yes")
        self.prompt("ok")
        self.assertEqual(self.est_posts, [])
        self.assertEqual(ds.send_estimates_choice({"kind": "estimates_on", "prompt": "yes", "data": {"agent_id": AID}},
                                                  {"pid": "p", "prompt": "yes", "typed_at": 1}), [])

    def test_the_unanswered_ask_is_shown_once_a_session(self):
        self.assertTrue(ds.est_ask_once("sess-a"))
        self.assertFalse(ds.est_ask_once("sess-a"))
        self.assertTrue(ds.est_ask_once("sess-b"))


class HookOrder(AsksBase):
    """The estimates note comes after every other prompt hook and never takes their place."""

    def setUp(self):
        super().setUp()
        self.env.setdefault("CLAUDE_CODE_ENTRYPOINT", os.environ.get("CLAUDE_CODE_ENTRYPOINT"))
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"

    def test_a_typed_answer_that_mentions_whats_next_still_answers_its_ask(self):
        self.prompt("show asks")
        note = self.prompt("3: yes, what's next")
        self.assertIn("ask 3, from Gamma", note)
        self.assertEqual([(b["instance"], b["answer"]) for b in self.sent()], [("cccccccc-0000-4000-8000-000000000001", "yes, what's next")])
        self.assertIn("datastash estimates:", note)
        self.assertLess(note.index("ask 3, from Gamma"), note.index("datastash estimates:"))

    def test_the_welcome_back_list_still_shows_on_a_build_list_prompt(self):
        self.prompt("hello")
        old = time.time() - ds.WELCOME_GAP - 5
        os.utime(ds._turn_path("sess1"), (old, old))
        note = self.prompt("what's next on the build list?")
        self.assertIn(ds.WELCOME_HEAD, self.last_hook["systemMessage"])
        self.assertIn("datastash estimates:", note)

    def test_how_long_and_typed_estimate_notes(self):
        self.assertIn("datastash estimate <card ids>", self.prompt("how long will XLI-12 take?"))
        self.assertIn("Run `datastash estimate XLI-12 #4`", self.prompt("DDS estimate XLI-12 #4"))
        self.assertNotIn("datastash estimates:", self.prompt("fix the login bug") or "")


if __name__ == "__main__":
    unittest.main()
