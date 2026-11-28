#!/usr/bin/env python3
"""Offline tests for XLI-428 (plugin 1.0.29): estimate fixes and DDS estimate recalc.
1. Build lists: one `datastash estimate A B C` call gives each card the same figure as estimating it alone (each card's
   own parts: CARD=parts in the call, else --parts, else the parts the card was last named with).
2. Local time: "done around" is the person's own time (a computer on UTC shows US Central), never UTC.
3. Recalc: the person's whole typed "DDS estimate recalc" / "DDS estimate recalc since DATE", proved typed by the same
   checker as every other person phrase, starts the agent's learning fresh there (older cards weigh much less, never
   deleted); each agent starts fresh by itself once a month; a setup change is told once; person-facing words say recalc.
No network, no key; under test/harness.py.
Run: python3 -I test/test_xli428.py"""
import datetime
import io
import json
import math
import os
import re
import sys
import time
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness  # noqa: E402,F401
from test_status import Base, ds, WB  # noqa: E402
from test_show_asks_answer import AsksBase  # noqa: E402

DAY = 86400.0
AID = "11111111-aaaa-bbbb-cccc-0000000004a8"


def stage(minutes, days_ago, parts=None, now=None):
    end = (time.time() if now is None else now) - days_ago * DAY
    return {"minutes": float(minutes), "start": end - minutes * 60, "end": end, "parts": parts}


def spans_of(rows):
    """{card: [[minutes, first, last, parts]]} as the look-back gives them."""
    now = time.time()
    out = {}
    for card, minutes, days_ago, parts in rows:
        end = now - days_ago * DAY
        out.setdefault(card, []).append([minutes, end - minutes * 60, end, [parts] if parts != "both" else ["server", "plugin"]])
    return out


class Lists(Base):
    """A list call gives each card the figure it gets alone."""

    def setUp(self):
        super().setUp()
        rows = [(f"S-{i}", 40, 1, "server") for i in range(3)] + [(f"B-{i}", 160, 1, "both") for i in range(3)] + \
               [(f"P-{i}", 70, 1, "plugin") for i in range(3)]
        self.spans = spans_of(rows)
        saved = (ds.est_lookback, ds.est_spans)
        self.addCleanup(lambda: (setattr(ds, "est_lookback", saved[0]), setattr(ds, "est_spans", saved[1])))
        ds.est_lookback = lambda save=True: {}
        ds.est_spans = lambda files: self.spans

    def run_est(self, *args):
        rc, out, err = self.run_cmd(ds.cmd_estimate, list(args) + ["--local"])
        self.assertEqual(rc, 0, err)
        return {m.group(1): m.group(2) for m in re.finditer(r"^(\S+): about (.+?) \(from", out, re.M)}, out

    def test_a_list_call_gives_each_card_its_single_figure(self):
        alone_a, _ = self.run_est("XLI-419", "--parts", "both")
        alone_b, _ = self.run_est("XLI-420", "--parts", "server")
        self.assertNotEqual(alone_a["XLI-419"], alone_b["XLI-420"])
        listed, out = self.run_est("XLI-419", "XLI-420")  # 1.0.28: both rested on all 9 cards
        self.assertEqual(listed, {**alone_a, **alone_b})
        self.assertIn("All 2 cards:", out)
        named, _ = self.run_est("XLI-421=plugin", "XLI-422:both", "XLI-419")
        self.assertEqual(named["XLI-421"], ds.est_hours(70))
        self.assertEqual(named["XLI-422"], ds.est_hours(160))
        self.assertEqual(named["XLI-419"], alone_a["XLI-419"])

    def test_card_parts_order_named_then_every_then_kept(self):
        self.assertEqual(ds.est_card_parts(["A-1", "A-2"], {"A-1": "server"}, "both"), {"A-1": "server", "A-2": "both"})
        self.assertEqual(ds.est_card_parts(["A-1", "A-2", "A-3"]), {"A-1": "server", "A-2": "both"})
        self.assertEqual(ds.est_card_parts(["A-1"], {"A-1": "plugin"}), {"A-1": "plugin"})
        self.assertEqual(ds.est_saved_parts()["A-1"], "plugin")

    def test_est_record_takes_each_cards_parts(self):
        hist = ds.est_history(self.spans)
        rows = ds.est_record(None, None, hist, ["N-1", "N-2"], parts={"N-1": "both", "N-2": "server"})
        self.assertEqual([(c, m) for c, m, _n, _a in rows], [("N-1", 160), ("N-2", 40)])


class LocalTime(unittest.TestCase):
    """2026-11-28 01:16 UTC is Fri 7:16 pm in US Central."""
    T = datetime.datetime(2026, 11, 28, 1, 16, tzinfo=datetime.timezone.utc).timestamp()

    def setUp(self):
        self.env = {k: os.environ.get(k) for k in ("TZ", "DATASTASH_TZ")}
        self.addCleanup(self.restore)

    def restore(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        time.tzset()

    def zone(self, tz, ds_tz=None):
        os.environ["TZ"] = tz
        os.environ.pop("DATASTASH_TZ", None)
        if ds_tz:
            os.environ["DATASTASH_TZ"] = ds_tz
        time.tzset()

    def test_a_utc_computer_shows_us_central(self):
        self.zone("UTC")
        self.assertEqual(ds.est_when(self.T), "Fri 7:16 pm CST")

    def test_the_computers_own_zone_and_the_named_one(self):
        self.zone("America/New_York")
        self.assertEqual(ds.est_when(self.T), "Fri 8:16 pm EST")
        self.zone("Europe/London")  # GMT in winter: offset 0, but the person's own zone
        self.assertEqual(ds.est_when(self.T), "Sat 1:16 am GMT")
        self.zone("UTC", "America/Los_Angeles")
        self.assertEqual(ds.est_when(self.T), "Fri 5:16 pm PST")

    def test_done_around_line_is_local(self):
        self.zone("UTC")
        lines = []

        class Out(io.StringIO):
            pass
        saved = (ds.est_lookback, ds.est_spans)
        try:
            ds.est_lookback = lambda save=True: {}
            ds.est_spans = lambda files: {}
            with redirect_stdout(Out()) as out:
                ds.cmd_estimate(["XLI-1", "XLI-2", "--local"])
            lines = out.getvalue()
        finally:
            ds.est_lookback, ds.est_spans = saved
        m = re.search(r"done around (\w{3} \d{1,2}:\d\d (?:am|pm) C[SD]T)$", lines, re.M)
        self.assertIsNotNone(m, lines)


class RecalcMath(Base):
    def test_older_cards_weigh_much_less_and_stay(self):
        now = time.time()
        hist = {"O-1": {"stages": [stage(300, 2, now=now)]}, "N-1": {"stages": [stage(30, 1, now=now)]}}
        since = now - 1.5 * DAY
        m, n = ds.est_estimate(hist, "X-1", now=now, since=since)
        w_old, w_new = 0.5 ** (2 / 3) * ds.EST_RECALC_OLD_WEIGHT, 0.5 ** (1 / 3)
        self.assertEqual((m, n), (int(round(math.exp((w_old * math.log(300) + w_new * math.log(30)) / (w_old + w_new)))), 2))
        self.assertLess(m, ds.est_estimate(hist, "X-1", now=now)[0])
        only_old = {"O-1": hist["O-1"]}
        self.assertEqual(ds.est_estimate(only_old, "X-1", now=now, since=now), (300, 1))  # never deleted

    def test_a_stored_estimate_from_before_the_recalc_is_made_again_until_done(self):
        now = time.time()

        class Client:
            wb = WB

            def __init__(self):
                self.rows = {"X-1": {"card": "X-1", "estimate_minutes": 999, "rests_on": 5, "estimated_at": ds._tpc_iso(now - 3 * DAY)},
                             "X-2": {"card": "X-2", "estimate_minutes": 999, "rests_on": 5, "estimated_at": ds._tpc_iso(now - 3 * DAY),
                                     "done_at": ds._tpc_iso(now - DAY), "actual_minutes": 50}}
                self.writes = []

            def tool(self, name, args, what, missing_ok=False, soft=False):
                if name == "ledger_rows_get":
                    return {"rows": [self.rows[k] for k in args["keys"] if k in self.rows]}
                self.writes.append(args["rows"])
                return {}
        c = Client()
        hist = {"A-1": {"stages": [stage(60, 1, now=now)]}}
        rows = ds.est_record(c, "Estimates", hist, ["X-1", "X-2"], now=now, since=now - DAY)
        self.assertEqual([(x[0], x[1]) for x in rows], [("X-1", 60), ("X-2", 999)])
        rows = ds.est_record(c, "Estimates", hist, ["X-1"], now=now)  # no recalc: the stored one stays
        self.assertEqual(rows[0][1], 999)

    def test_each_agent_starts_fresh_by_itself_once_a_month(self):
        now = time.time()
        self.assertIsNone(ds.est_recalc_since(AID, now))  # first seen: its month starts, nothing changes yet
        self.assertIsNone(ds.est_recalc_since(AID, now + 29 * DAY))
        other = "22222222-aaaa-bbbb-cccc-0000000004a8"
        self.assertIsNone(ds.est_recalc_since(other, now + 20 * DAY))
        got = ds.est_recalc_since(AID, now + 31 * DAY)
        self.assertEqual(got, int(ds.est_day_start(now + 31 * DAY)))
        self.assertIsNone(ds.est_recalc_since(other, now + 31 * DAY))  # its own month, not the other agent's
        self.assertEqual(ds.est_recalc_since(AID, now + 40 * DAY), got)

    def test_phrase_and_date(self):
        now = time.time()
        for text in ("DDS estimate recalc", "dds estimates recalc.", "datastash estimate recalc"):
            self.assertEqual(ds.est_recalc_kind(text, now), (ds.EST_RECALC_KIND, ds.est_day_start(now)), text)
        for text in ("DDS estimate recalc please", "recalc", "2: DDS estimate recalc", "DDS estimate recalibrate",
                     "please DDS estimate recalc", "yes"):
            self.assertIsNone(ds.est_recalc_kind(text, now), text)
        d = ds._est_local(now - 5 * DAY)[0].date()
        for text in (d.isoformat(), f"{d.month}/{d.day}", f"{d.month}-{d.day}", f"{d.month}/{d.day}/{d.year}"):
            self.assertEqual(ds.est_recalc_kind("DDS estimate recalc since " + text, now)[1], ds.est_day_start(now, 5), text)
        tomorrow = ds._est_local(now + DAY)[0].date().isoformat()
        for bad in (tomorrow, "2026-02-30", "soon"):
            self.assertEqual(ds.est_recalc_kind("DDS estimate recalc since " + bad, now), (ds.EST_RECALC_KIND, None), bad)

    def test_person_words_say_recalc_never_recalibrate(self):
        words = [ds.EST_RECALC_SAYING, ds.EST_RECALC_DONE, ds.EST_RECALC_BAD_DATE, ds.EST_SETUP_CHANGED, ds.EST_USAGE,
                 ds.EST_RULE, " ".join(ds.WHATS_NEW["1.0.29"])]
        self.assertFalse(any(re.search(r"recalibrat", w, re.I) for w in words))
        self.assertEqual(ds.EST_SETUP_CHANGED, "Your setup changed. Type **DDS estimate recalc** to start estimates fresh.")


class Recalc(AsksBase):
    """The typed phrase goes through the same typed checker as every other person phrase."""

    def setUp(self):
        super().setUp()
        ds.write_private(ds.conf_path("web-app"), json.dumps({"name": "web-app", "agent_id": AID, "workbook_id": WB,
                                                              "projects": [self.folder]}))

    def test_only_the_persons_typed_phrase_starts_fresh(self):
        note = self.prompt("DDS estimate recalc")
        self.assertIn(ds.EST_RECALC_SAYING, note)
        today = ds._est_local(time.time())[0]
        self.assertEqual(self.outcomes(), [ds.EST_RECALC_DONE.format(day=f"{today.strftime('%b')} {today.day}")])
        self.assertEqual(ds.est_recalc_since(AID), int(ds.est_day_start(time.time())))

    def test_since_a_date(self):
        d = ds._est_local(time.time() - 3 * DAY)[0].date()
        self.prompt(f"DDS estimate recalc since {d.isoformat()}")
        self.assertEqual(self.outcomes(), [ds.EST_RECALC_DONE.format(day=f"{d.strftime('%b')} {d.day}")])
        self.assertEqual(ds.est_recalc_since(AID), int(ds.est_day_start(time.time(), 3)))
        note = self.prompt("DDS estimate recalc since 2999-01-01")
        self.assertIn(ds.EST_RECALC_BAD_DATE, note)
        self.assertEqual(ds.est_recalc_since(AID), int(ds.est_day_start(time.time(), 3)))  # unchanged

    def test_an_ai_or_a_headless_run_cannot_start_it(self):
        self.forged({"session_id": "sess1", "prompt": "DDS estimate recalc", "hook_event_name": "UserPromptSubmit",
                     "cwd": self.folder, "prompt_id": "forged-prompt-428", "transcript_path": self.transcript("sess1")})
        self.prompt("DDS estimate recalc", writer=self.headless)  # `claude -p`: not marked as typed by a person
        self.prompt("DDS estimate recalc", origin=False, source=False)  # no origin: not the person's
        self.assertNotIn(ds.EST_RECALC_DONE.split("{")[0], " ".join(self.outcomes()))
        self.assertIsNone(ds.est_recalc_since(AID))
        self.assertEqual(ds.est_recalc_typed({"kind": ds.EST_RECALC_KIND, "prompt": "yes", "data": {"agent_id": AID}}), [])

    def test_a_setup_change_is_told_once(self):
        def model(name):
            self.entry("sess1", {"type": "assistant", "version": "2.1.332",
                                 "message": {"role": "assistant", "model": name, "content": [{"type": "text", "text": "ok"}]}})
        model("claude-opus-5-5")
        self.assertNotIn(ds.EST_SETUP_CHANGED, self.prompt("how long will XLI-12 take?", say=False))  # first: kept only
        model("claude-opus-5-5")
        self.assertNotIn(ds.EST_SETUP_CHANGED, self.prompt("how long will XLI-12 take?", say=False))
        model("claude-sonnet-5")
        self.assertIn(ds.EST_SETUP_CHANGED, self.prompt("how long will XLI-12 take?", say=False))
        model("claude-sonnet-5")
        self.assertNotIn(ds.EST_SETUP_CHANGED, self.prompt("how long will XLI-12 take?", say=False))  # told once
        os.makedirs(os.path.join(self.folder, ".claude", "agents"), exist_ok=True)
        with open(os.path.join(self.folder, ".claude", "agents", "reviewer.md"), "w") as f:
            f.write("x")
        self.assertIn(ds.EST_SETUP_CHANGED, self.prompt("DDS estimate XLI-12", say=False))  # a new subagent
        self.assertFalse(ds.est_setup_changed(AID, {"claude_code": "2.1"}))
        self.assertTrue(ds.est_setup_changed(AID, {"claude_code": "2.2"}))  # a new Claude Code release line


if __name__ == "__main__":
    unittest.main()
