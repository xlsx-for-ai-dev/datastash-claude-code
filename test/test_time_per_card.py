#!/usr/bin/env python3
"""Offline tests for XLI-378 E (plugin 1.0.5): time per card, the plugin side of ledger_feeds_setup services ["burn"].
- Off until `datastash time-per-card on` in the folder (which sets up the tabs); the hooks then start a background send
  and return at once; off again sends nothing.
- The reader: one row per card, activity and model; a turn's tokens counted once (its usage is on every line of it);
  minutes from a turn's input to its end and from a tool call to its result, never the time before the person types;
  wakes and retries counted; activities by the fixed rules; the card from a subagent's description, DATASTASH_CARD or
  the branch; a review subagent's calls are reviewing; card ids are letters, a dash and digits only.
- Privacy: a canary in prompts, answers, commands, file paths and tool results is never sent.
- Each send has an idempotency key; offsets move only after datastash took the rows; a send cut short is sent again with
  the same rows and keys, never counted twice; only new lines are sent the next time.
- The fold (carry-over a): past the 10,000-row limit, the oldest finished cards (never one in this report) are folded:
  their Burn sums go into the Cards cost folded_* cells with inc and their Burn rows are deleted; a fold cut short is
  finished from its journal without counting twice; a fold problem never loses the sent rows.
No network, no key; a fake datastash in memory, under test/harness.py.
Run: python3 -I test/test_time_per_card.py"""
import io
import json
import os
import sys
import threading
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import ds, TMP  # noqa: E402  (the same harness and plugin copy)
import harness  # noqa: E402

CANARY = "TPC_CANARY_51ab"
WB = "0b6a3f9e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
KEY = "ldgr_ag_testkeytestkeytestkey"
SID = "5b6b3ff3-d567-4579-81fc-a8eedc898995"
MODEL = "claude-opus-5-5"


class Fake:
    """Keyed tabs with inc/min/max, idempotency keys, filter (eq, ne, blank, order, rows_matched), delete."""

    KEYS = {"Burn": "key", "Cards cost": "card", "Prices": "model"}

    def __init__(self):
        self.tabs, self.seen_idem, self.calls, self.fail_next = {}, {}, [], []
        self.lock = threading.RLock()

    def apply(self, tab, row):
        rows = self.tabs.setdefault(tab, {})
        k = row[self.KEYS[tab]]
        cur = dict(rows.get(k) or {})
        for f, v in row.items():
            if isinstance(v, dict) and len(v) == 1:
                (op, arg), = v.items()
                if op == "inc":
                    cur[f] = (cur.get(f) or 0) + arg
                elif op == "min":
                    cur[f] = arg if not cur.get(f) else min(cur[f], arg)
                elif op == "max":
                    cur[f] = arg if not cur.get(f) else max(cur[f], arg)
                continue
            cur[f] = v
        rows[k] = cur

    @staticmethod
    def match(c, w):
        if not w:
            return True
        if "and" in w:
            return all(Fake.match(c, x) for x in w["and"])
        if "eq" in w:
            return c.get(w["eq"][0]) == w["eq"][1]
        if "ne" in w:
            v = c.get(w["ne"][0])
            return v is not None and v != w["ne"][1]
        if "blank" in w:
            return c.get(w["blank"][0]) in (None, "")
        raise AssertionError(w)

    def mcp_tool(self, key, name, args, timeout=60):
        assert key == KEY
        with self.lock:
            self.calls.append((name, json.loads(json.dumps(args))))
            if self.fail_next:
                f = self.fail_next.pop(0)
                if f is not None:
                    if isinstance(f, Exception):
                        raise f
                    return f
            tab = args.get("tab")
            if name == "ledger_feeds_setup":
                return False, {"feeds": [{"service": "burn", "ok": True, "tabs": []}]}
            if name == "ledger_rows_upsert":
                idem = args.get("idempotency_key")
                if idem and idem in self.seen_idem:
                    return False, self.seen_idem[idem]
                for r in args["rows"]:
                    self.apply(tab, r)
                out = {"saved": len(args["rows"])}
                if idem:
                    self.seen_idem[idem] = out
                return False, out
            if name == "ledger_rows_filter":
                if tab not in self.tabs:
                    return True, {"code": "not_found", "message": "no such tab"}
                rows = [dict(c) for c in self.tabs[tab].values() if self.match(c, args.get("where"))]
                for o in reversed(args.get("order") or []):
                    rows.sort(key=lambda c: str(c.get(o["column"]) or ""), reverse=o.get("dir") == "desc")
                start = int(args.get("cursor") or 0)
                page = rows[start:start + args.get("limit", 1000)]
                out = {"rows": page, "rows_matched": len(rows)}
                if start + len(page) < len(rows):
                    out["cursor"] = str(start + len(page))
                return False, out
            if name == "ledger_rows_delete":
                for k in args["keys"]:
                    self.tabs.get(tab, {}).pop(k, None)
                return False, {"deleted": len(args["keys"])}
            raise AssertionError(name)

    def sent_text(self):
        return json.dumps([a for n, a in self.calls])


def ts(sec):
    return f"2026-11-24T10:{sec // 60:02d}:{sec % 60:02d}.000Z"


class Log:
    """A transcript written the way Claude Code writes it: one JSON object per line."""

    def __init__(self, path, branch="xli-378e"):
        self.path, self.branch, self.n = path, branch, 0
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "a").close()

    def put(self, e):
        e.setdefault("gitBranch", self.branch)
        with open(self.path, "a") as f:
            f.write(json.dumps(e) + "\n")

    def prompt(self, at, text=f"please fix it {CANARY}", kind="human"):
        self.put({"type": "user", "timestamp": ts(at), "origin": {"kind": kind}, "message": {"role": "user", "content": text}})

    def turn(self, at, tools=(), usage=(10, 20, 300, 40), mid=None, model=MODEL, end=None, text=f"ok {CANARY}"):
        """One model turn: a text line, then one line per tool call, each carrying the same usage (as Claude Code does)."""
        self.n += 1
        mid = mid or f"msg_{self.n}"
        u = {"input_tokens": usage[0], "output_tokens": usage[1], "cache_read_input_tokens": usage[2],
             "cache_creation_input_tokens": usage[3]}
        self.put({"type": "assistant", "timestamp": ts(at), "message": {"id": mid, "model": model, "usage": u,
                                                                         "content": [{"type": "text", "text": text}]}})
        ids = []
        for i, (name, args) in enumerate(tools):
            tid = f"tu_{self.n}_{i}"
            ids.append(tid)
            self.put({"type": "assistant", "timestamp": ts(end if end is not None else at), "message": {
                "id": mid, "model": model, "usage": u, "content": [{"type": "tool_use", "id": tid, "name": name, "input": args}]}})
        return ids

    def result(self, at, tid, error=False):
        self.put({"type": "user", "timestamp": ts(at), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "is_error": error, "content": f"output {CANARY}"}]}})


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.saved = {}
        for name, new in (("mcp_tool", self.fake.mcp_tool),):
            self.saved[name] = getattr(ds, name)
            setattr(ds, name, new)
        self.saved_sleep = ds.CiClient.__dict__["sleep"]
        ds.CiClient.sleep = staticmethod(lambda s: None)
        self.proj = os.path.join(ds.CLAUDE_DIR, "projects", "-tmp-proj")
        self.main = os.path.join(self.proj, SID + ".jsonl")
        for p in [self.proj]:
            harness.remove_tree(p)
        ds.ensure_dirs()
        for n in os.listdir(ds.STATE):
            if n.startswith("tpc-"):
                os.unlink(os.path.join(ds.STATE, n))
        os.environ.pop("DATASTASH_CARD", None)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(ds, k, v)
        ds.CiClient.sleep = self.saved_sleep
        harness.remove_tree(self.proj)

    def run_once(self, ended=False):
        return ds.tpc_run(SID, self.main, ended, KEY, WB)

    def burn(self):
        return self.fake.tabs.get("Burn", {})

    def cards(self):
        return self.fake.tabs.get("Cards cost", {})

    def sub(self, name, description, agent_type="general-purpose"):
        d = os.path.join(self.proj, SID, "subagents")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"agent-{name}.meta.json"), "w") as f:
            json.dump({"agentType": agent_type, "description": description}, f)
        return Log(os.path.join(d, f"agent-{name}.jsonl"), branch="main")


class Classify(Base):
    def test_card_ids_from_branches_and_descriptions(self):
        self.assertEqual(ds.tpc_card("xli-378e"), "XLI-378")
        self.assertEqual(ds.tpc_card("feature/ABC-12-login"), "ABC-12")
        self.assertEqual(ds.tpc_card("Build XLI-378 E"), "XLI-378")
        for none in ("main", "release-1.0.4", "fix-12", "v1.2", "", None):
            self.assertIsNone(ds.tpc_card(none), none)

    def test_activities(self):
        k = ds.tpc_tool_kind
        self.assertEqual(k("Edit", {"file_path": "/a"})[0], "coding")
        self.assertEqual(k("Bash", {"command": "cd /x && sed -i 's/a/b/' f"})[0], "coding")
        self.assertEqual(k("Bash", {"command": "cat > f <<'EOF'\nx\nEOF"})[0], "coding")
        self.assertEqual(k("Bash", {"command": "cd /x && npx vitest run a.test.ts 2>&1 | tail -5"})[0], "testing")
        self.assertEqual(k("Bash", {"command": "python3 -I test/test_plugin.py"})[0], "testing")
        self.assertEqual(k("Bash", {"command": "git push -u origin xli-1"})[0], "landing")
        self.assertEqual(k("Bash", {"command": "gh pr create --title t"})[0], "landing")
        self.assertEqual(k("Bash", {"command": "sleep 30"})[0], "wakes")
        self.assertEqual(k("Monitor", {})[0], "wakes")
        self.assertEqual(k("Bash", {"command": "cd /x && grep -n a b | head 2>/dev/null"})[0], "read")
        self.assertEqual(k("Bash", {"command": "gh pr view 12"})[0], "read")
        self.assertEqual(k("Read", {"file_path": "/a"})[0], "read")
        self.assertEqual(k("Agent", {"description": "Review PR 12"})[0], "reviewing")
        self.assertEqual(k("Agent", {"description": "Build XLI-1"})[0], "planning")
        self.assertEqual(k("Bash", {"command": "make"})[0], "planning")
        self.assertEqual(k("SendMessage", {})[0], "planning")


class Reader(Base):
    def test_tokens_once_per_turn_minutes_and_activities(self):
        log = Log(self.main)
        log.prompt(0)
        a = log.turn(10, [("Read", {"file_path": f"/src/{CANARY}.py"})], end=12)
        log.result(15, a[0])
        b = log.turn(20, [("Read", {"file_path": f"/src/{CANARY}.py"}), ("Edit", {"file_path": "/src/a.py"})], end=25)
        log.result(28, b[0])
        log.result(30, b[1])
        log.turn(40, [], text="done")
        log.prompt(400)  # the person typed 6 minutes later: never counted
        c = log.turn(410, [("Bash", {"command": f"pytest -q {CANARY}"})])
        log.result(470, c[0])
        r = self.run_once()
        self.assertEqual(r["burn"], 5)
        rows = {k.split(" / ")[2]: v for k, v in self.burn().items()}
        self.assertEqual(set(rows), {"orienting", "lookups", "coding", "testing", "planning"}, rows)
        # turn 1 (orienting): from the prompt at 0 to the turn's end at 12, then the read 12 -> 15
        self.assertAlmostEqual(rows["orienting"]["minutes"], 15 / 60, places=3)
        # turn 2 from the result at 15 to its end at 25, its repeat read 25 -> 28; the edit's result at 30 adds 2 s only
        self.assertAlmostEqual(rows["lookups"]["minutes"], 13 / 60, places=3)
        self.assertAlmostEqual(rows["coding"]["minutes"], 2 / 60, places=3)
        self.assertEqual(rows["orienting"]["tokens_in"], 10)
        self.assertEqual(rows["orienting"]["cache_read"], 300)
        self.assertEqual(rows["orienting"]["calls"], 1)
        # turn 2: tokens once, to its first tool call's activity (the repeat read: lookups); the edit is a coding call
        self.assertEqual(rows["lookups"]["tokens_out"], 20)
        self.assertEqual(rows["lookups"]["calls"], 1)
        coding = self.burn()[f"XLI-378 / - / coding / {MODEL}"]
        self.assertEqual(coding["calls"], 1)
        self.assertEqual(coding.get("tokens_in"), 0)
        # the text-only turn is planning; the person's 6 minutes are nowhere
        self.assertAlmostEqual(rows["planning"]["minutes"], 10 / 60, places=3)
        self.assertAlmostEqual(rows["testing"]["minutes"], (10 + 60) / 60, places=3)
        total = sum(v["minutes"] for v in self.burn().values())
        self.assertLess(total, 120 / 60)
        self.assertEqual({v["card"] for v in self.burn().values()}, {"XLI-378"})
        self.assertNotIn(CANARY, self.fake.sent_text())
        self.assertNotIn("/src/", self.fake.sent_text())

    def test_wakes_retries_and_overlapping_tools_counted_once(self):
        log = Log(self.main)
        log.prompt(0, text="<task-notification>done</task-notification>", kind="task-notification")
        log.turn(5, [], text="noted")
        log.prompt(10)
        a = log.turn(11, [("Bash", {"command": "git push origin x"})])
        log.result(12, a[0], error=True)
        b = log.turn(13, [("Bash", {"command": "git push origin x"}), ("Bash", {"command": "git push origin y"})])
        log.result(23, b[0])
        log.result(23, b[1])
        self.run_once()
        land = self.burn()[f"XLI-378 / - / landing / {MODEL}"]
        self.assertEqual(land["retries"], 1)
        self.assertEqual(land["calls"], 3)
        # turns 1 s + 1 s, tools 1 s + 10 s (two calls side by side count their 10 s once)
        self.assertAlmostEqual(land["minutes"], 13 / 60, places=3)
        self.assertAlmostEqual(self.burn()[f"XLI-378 / - / wakes / {MODEL}"]["minutes"], 5 / 60, places=3)
        self.assertEqual(self.cards()["XLI-378"]["wakes"], 1)
        self.assertEqual(self.cards()["XLI-378"]["retries"], 1)

    def test_cards_from_env_branch_and_subagents_and_review_subagents(self):
        log = Log(self.main, branch="main")
        log.prompt(0)
        log.turn(2, [("Edit", {"file_path": "/a"})])
        s1 = self.sub("a1", "Build XLI-77 part B")
        s1.prompt(3, text=f"brief {CANARY}")
        x = s1.turn(4, [("Edit", {"file_path": "/b"})])
        s1.result(6, x[0])
        s2 = self.sub("a2", "Opus review of PR 9 for ABC-5")
        s2.prompt(3)
        s2.turn(5, [("Read", {"file_path": "/c"}), ("Bash", {"command": "pytest"})])
        self.run_once(ended=True)
        keys = set(self.burn())
        self.assertIn(f"no card / - / coding / {MODEL}", keys)
        self.assertIn(f"XLI-77 / - / coding / {MODEL}", keys)
        self.assertIn(f"ABC-5 / - / reviewing / {MODEL}", keys)
        self.assertEqual(self.burn()[f"ABC-5 / - / reviewing / {MODEL}"]["calls"], 2)
        self.assertEqual(self.cards()["XLI-77"]["subagents"], 1)
        self.assertEqual(self.cards()["ABC-5"]["subagents"], 1)
        self.assertEqual(self.cards()["no card"]["sessions"], 1)
        os.environ["DATASTASH_CARD"] = "web-42"
        self.addCleanup(os.environ.pop, "DATASTASH_CARD", None)
        log.turn(30, [("Edit", {"file_path": "/a"})])
        self.run_once()
        self.assertIn(f"WEB-42 / - / coding / {MODEL}", set(self.burn()))
        self.assertEqual(self.cards()["XLI-77"]["subagents"], 1)  # a subagent is counted once
        for k, v in self.burn().items():
            self.assertRegex(v["card"], r"^(?:[A-Z]{2,10}-\d+|no card)$")


class Sending(Base):
    def test_only_new_lines_and_nothing_twice(self):
        log = Log(self.main)
        log.prompt(0)
        log.turn(5, [("Edit", {"file_path": "/a"})])
        self.run_once()
        first = dict(self.burn()[f"XLI-378 / - / coding / {MODEL}"])
        n = len(self.fake.calls)
        self.assertEqual(self.run_once()["burn"], 0)  # nothing new: nothing sent
        self.assertEqual([c for c in self.fake.calls[n:] if c[0] == "ledger_rows_upsert"], [])
        log.turn(9, [("Edit", {"file_path": "/a"})])
        self.run_once()
        self.assertEqual(self.burn()[f"XLI-378 / - / coding / {MODEL}"]["calls"], first["calls"] + 1)
        ups = [a for n_, a in self.fake.calls if n_ == "ledger_rows_upsert"]
        self.assertTrue(all(a.get("idempotency_key") for a in ups))

    def test_a_cut_short_send_is_sent_again_with_the_same_keys(self):
        log = Log(self.main)
        log.prompt(0)
        log.turn(5, [("Edit", {"file_path": "/a"})])
        # the Burn page lands, then the Cards cost write fails for good: the run stops, the offsets stay
        self.fake.fail_next = [None, (True, {"code": "refused", "message": "no"})]
        with self.assertRaises(ds.CiProblem):
            self.run_once()
        self.assertEqual(self.burn()[f"XLI-378 / - / coding / {MODEL}"]["calls"], 1)
        log.turn(8, [("Edit", {"file_path": "/a"})])  # more lines meanwhile
        self.run_once()
        # the pending send went again (its Burn page deduplicated by its key), then the new line
        self.assertEqual(self.burn()[f"XLI-378 / - / coding / {MODEL}"]["calls"], 2)
        self.assertIn("XLI-378", self.cards())

    def test_a_half_written_line_waits(self):
        log = Log(self.main)
        log.prompt(0)
        log.turn(5, [("Edit", {"file_path": "/a"})])
        with open(self.main, "a") as f:
            f.write('{"type": "assistant", "gitBranch": "xli-378e", "timestamp": "2026')
        self.run_once()
        with open(self.main, "a") as f:
            f.write('-11-24T10:00:09.000Z", "message": {"id": "m9", "model": "' + MODEL + '", "content": '
                    '[{"type": "tool_use", "id": "t9", "name": "Edit", "input": {}}]}}\n')
        self.run_once()
        self.assertEqual(self.burn()[f"XLI-378 / - / coding / {MODEL}"]["calls"], 2)

    def test_files_outside_claude_projects_are_never_read(self):
        outside = os.path.join(harness.tmpdir(self, dir=TMP), SID + ".jsonl")
        log = Log(outside)
        log.prompt(0)
        log.turn(5, [("Edit", {"file_path": "/a"})])
        self.assertEqual(ds.tpc_files(outside, SID), [])
        self.assertEqual(ds.tpc_run(SID, outside, True, KEY, WB)["burn"], 0)
        self.assertEqual(self.fake.calls, [])

    def test_the_background_run_logs_one_line_and_never_raises(self):
        ds.write_private(ds.conf_path("web-app"), json.dumps({"name": "web-app", "workbook_id": WB}))
        old = ds.read_key
        ds.read_key = lambda s: KEY
        self.addCleanup(setattr, ds, "read_key", old)
        log = Log(self.main)
        log.prompt(0)
        log.turn(5, [("Edit", {"file_path": f"/{CANARY}"})])
        self.assertEqual(ds.cmd_time_per_card(["run", "web-app", SID, self.main, "1"]), 0)
        self.fake.fail_next = [KeyError("boom")]  # not a network error: never retried
        log.turn(9, [("Edit", {"file_path": "/a"})])
        self.assertEqual(ds.cmd_time_per_card(["run", "web-app", SID, self.main, "0"]), 0)
        with open(ds.log_path("web-app")) as f:
            lines = [x for x in f.read().splitlines() if "time per card" in x]
        self.assertIn("time per card: sent 1 Burn and 1 Cards cost rows", lines[-2])
        self.assertIn("time per card: not sent:", lines[-1])
        self.assertNotIn(CANARY, "\n".join(lines))
        self.assertEqual(ds.cmd_time_per_card(["run", "../x", SID, self.main, "1"]), 1)


class Fold(Base):
    def setUp(self):
        super().setUp()
        self.old_at = ds.TPC_FOLD_AT
        ds.TPC_FOLD_AT = 4
        self.journal = os.path.join(ds.STATE, f"tpc-fold-{WB}.json")

    def tearDown(self):
        ds.TPC_FOLD_AT = self.old_at
        super().tearDown()

    def seed(self, card, last, rows):
        for act, minutes, toks, cost in rows:
            self.fake.apply("Burn", {"key": f"{card} / - / {act} / seed-model", "card": card, "activity": act, "model": "seed-model",
                                     "minutes": minutes, "calls": 1, "tokens": toks, "tokens_in": toks, "est_cost": cost})
        self.fake.apply("Cards cost", {"card": card, "last_seen": last})

    def test_past_the_limit_the_oldest_finished_cards_fold_and_read_the_same(self):
        self.seed("OLD-1", "2026-11-01T00:00:00.000Z", [("coding", 3, 100, 0.5), ("reviewing", 2, 50, {"cant_calculate": "x"})])
        self.seed("OLD-2", "2026-11-02T00:00:00.000Z", [("testing", 1, 10, 0.1)])
        self.seed("NEW-3", "2026-11-03T00:00:00.000Z", [("coding", 1, 1, 0)])
        log = Log(self.main, branch="old-1")  # this report's card is OLD-1: never folded now
        log.prompt(0)
        log.turn(5, [("Edit", {"file_path": "/a"})])
        r = self.run_once()  # Burn: 4 seeded + 1 new = 5 rows > 4
        self.assertEqual(r["folded"], ["OLD-2"])
        self.assertNotIn("OLD-2 / - / testing / seed-model", self.burn())
        self.assertEqual(self.cards()["OLD-2"], {"card": "OLD-2", "last_seen": "2026-11-02T00:00:00.000Z", "folded_minutes": 1,
                                                 "folded_calls": 1, "folded_tokens": 10, "folded_progress_tokens": 10,
                                                 "folded_est_cost": 0.1})
        self.assertIn("OLD-1 / - / coding / seed-model", self.burn())
        self.assertFalse(os.path.exists(self.journal))
        # next report on another card: OLD-1 is the oldest unfolded one now
        os.environ["DATASTASH_CARD"] = "NEW-3"
        self.addCleanup(os.environ.pop, "DATASTASH_CARD", None)
        log.turn(9, [("Edit", {"file_path": "/a"})])
        self.fake.apply("Burn", {"key": "NEW-3 / - / testing / x", "card": "NEW-3", "activity": "testing", "model": "x", "minutes": 1})
        r = self.run_once()
        self.assertEqual(r["folded"], ["OLD-1"])
        old1 = self.cards()["OLD-1"]
        # the seeded rows (tokens from the engine's column; a can't-calculate est_cost left out) plus the reported row
        # (no tokens column in this fake: its four counts, 10 + 20 + 300 + 40)
        self.assertEqual((old1["folded_calls"], old1["folded_tokens"], old1["folded_progress_tokens"], old1["folded_est_cost"]),
                         (3, 100 + 50 + 370, 100 + 370, 0.5))
        self.assertAlmostEqual(old1["folded_minutes"], 3 + 2 + 5 / 60, places=3)
        self.assertFalse([k for k in self.burn() if k.startswith("OLD-1 /")])

    def test_under_the_limit_nothing_folds(self):
        self.seed("OLD-1", "2026-11-01T00:00:00.000Z", [("coding", 3, 100, 0.5)])
        log = Log(self.main)
        log.prompt(0)
        log.turn(5, [("Edit", {"file_path": "/a"})])
        self.assertEqual(self.run_once()["folded"], [])
        self.assertIn("OLD-1 / - / coding / seed-model", self.burn())

    def test_a_fold_cut_short_is_finished_once(self):
        for i in range(4):
            self.seed(f"OLD-{i}", f"2026-11-0{i + 1}T00:00:00.000Z", [("coding", 1, 10, 0)])
        log = Log(self.main)
        log.prompt(0)
        log.turn(5, [("Edit", {"file_path": "/a"})])
        # the send's two upserts and the count, the oldest list and the card's rows go through; its upsert too; the delete fails
        boom = (True, {"code": "refused", "message": "down"})
        orig = self.fake.mcp_tool

        def flaky(key, name, args, timeout=60):
            if name == "ledger_rows_delete" and not getattr(flaky, "done", False):
                flaky.done = True
                return boom
            return orig(key, name, args, timeout)
        ds.mcp_tool = flaky
        r = self.run_once()
        self.assertTrue(r["fold_problem"])
        self.assertTrue(os.path.exists(self.journal))
        self.assertEqual(self.cards()["OLD-0"]["folded_tokens"], 10)
        self.assertIn("OLD-0 / - / coding / seed-model", self.burn())  # the rows sent are kept; the fold waits
        log.turn(9, [("Edit", {"file_path": "/a"})])
        self.run_once()
        self.assertEqual(self.cards()["OLD-0"]["folded_tokens"], 10)  # not twice
        self.assertNotIn("OLD-0 / - / coding / seed-model", self.burn())
        self.assertFalse(os.path.exists(self.journal))


class OptIn(Base):
    def setUp(self):
        super().setUp()
        self.folder = os.path.realpath(harness.tmpdir(self, prefix="proj-", dir=TMP))
        ds.write_private(ds.conf_path("web-app"), json.dumps({"name": "web-app", "workbook_id": WB,
                                                              "agent_id": "a1b2c3d4-0000-4000-8000-000000000001",
                                                              "projects": [self.folder]}))
        ds.write_private(ds.key_path("web-app"), KEY + "\n")
        self.spawned = []
        self.saved["spawn_detached"] = ds.spawn_detached
        ds.spawn_detached = lambda argv, cwd: self.spawned.append((argv, cwd))
        self.saved["read_key"] = ds.read_key
        ds.read_key = lambda s: KEY
        self.env = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = self.folder
        if os.path.exists(ds.TPC_PATH):
            os.unlink(ds.TPC_PATH)

    def tearDown(self):
        if self.env is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = self.env
        super().tearDown()

    def hook(self, event):
        inp = {"session_id": SID, "transcript_path": self.main, "hook_event_name": event, "cwd": self.folder}
        old = sys.stdin
        sys.stdin = io.StringIO(json.dumps(inp))
        try:
            return ds.cmd_time_per_card(["hook"])
        finally:
            sys.stdin = old

    def say(self, *args):
        with redirect_stdout(io.StringIO()) as out:
            rc = ds.cmd_time_per_card(list(args))
        return rc, out.getvalue()

    def test_off_until_on_then_the_hooks_start_a_background_send(self):
        self.assertEqual(self.hook("SubagentStop"), 0)
        self.assertEqual(self.spawned, [])
        rc, out = self.say()
        self.assertIn("time per card is off", out)
        rc, out = self.say("on")
        self.assertEqual(rc, 0, out)
        self.assertIn("time per card is on", out)
        self.assertNotIn("cost", out)
        self.assertEqual([a for n, a in self.fake.calls if n == "ledger_feeds_setup"], [{"workbook_id": WB, "services": ["burn"]}])
        self.assertEqual(self.hook("SubagentStop"), 0)
        self.assertEqual(self.hook("SessionEnd"), 0)
        self.assertEqual([a[0][-5:] for a in self.spawned], [["run", "web-app", SID, self.main, "0"],
                                                            ["run", "web-app", SID, self.main, "1"]])
        rc, out = self.say("off")
        self.assertIn("time per card is off", out)
        self.hook("SessionEnd")
        self.assertEqual(len(self.spawned), 2)

    def test_on_without_the_tabs_changes_nothing(self):
        self.fake.fail_next = [(True, {"code": "refused", "message": "Tab Burn is there with another key"})]
        rc, out = self.say("on")
        self.assertEqual(rc, 1)
        self.assertIn("not set up", out)
        self.assertFalse(ds.tpc_is_on(self.folder))

    def test_the_hooks_are_wired_beside_the_others(self):
        with open(os.path.join(HERE, "..", "plugins", "datastash", "hooks", "hooks.json")) as f:
            hooks = json.load(f)
        end = [h["args"][1:] for g in hooks["hooks"]["SessionEnd"] for h in g["hooks"]]
        self.assertEqual(end, [["end"], ["time-per-card", "hook"]])
        sub = [h for g in hooks["hooks"]["SubagentStop"] for h in g["hooks"]]
        self.assertEqual([h["args"][1:] for h in sub], [["time-per-card", "hook"]])
        self.assertTrue(all(h["timeout"] <= 10 and not h.get("async") for h in sub))
        self.assertIn("XLI-378 E: time per card", hooks["description"])


if __name__ == "__main__":
    unittest.main()
