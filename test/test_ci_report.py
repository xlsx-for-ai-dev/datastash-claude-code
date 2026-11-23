#!/usr/bin/env python3
"""Offline tests for XLI-378 B (plugin 0.9.11): `datastash ci-report`, per-test CI results into the "tests" preset tabs.
- Privacy: a canary in a failure's message, its text, system-out and system-err never reaches datastash or the output;
  only ids, counts, times, the error class and the hash of the first message line (numbers and hex ids stripped) do.
- The XML parser refuses a DOCTYPE, entity declarations (billion laughs) and external entities.
- Flaky is order-independent (card A review, cycle 2): a pass and a failure of one test on one commit flag it whichever
  report comes first, also when the legs ran different test sets, and when both read at once; flaky_commits is added
  exactly once. A test run in one shard only is never flaky; a failure then pass inside one report (a retry) is. A
  --local run never writes or reads the commit's flaky bookkeeping.
- Pages of 1,000 rows with an idempotency key each; a 429 or busy answer is waited out and the page sent again.
- main_broke_at on the default branch; base_avg_ms from four weeks back; old Test runs rows removed.
- The command always exits 0 with one line, also for bad arguments, a missing key, no network and a bad
  DATASTASH_CI_WAIT_SECONDS; a server that does not answer is given a short total time, not the brake wait.
No network, no key; a fake datastash in memory, under test/harness.py.
Run: python3 -I test/test_ci_report.py"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import ds, BIN, TMP  # noqa: E402  (the same harness and plugin copy)
import harness  # noqa: E402  (tmpdir: a test's own temp folders go when it ends)

CANARY = "MSG_CANARY_7f3e"
WB = "0b6a3f9e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
KEY = "ldgr_ag_testkeytestkeytestkey"
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


class FakeDatastash:
    """Keyed tabs with the ops the server applies (inc, min, max), idempotency keys, if_version, filter, get, delete."""

    def __init__(self):
        self.tabs, self.versions, self.seen_idem, self.calls = {}, {}, {}, []
        self.lock = threading.RLock()
        self.fail_next = []  # exceptions or (is_err, body) answers to give before the next calls
        self.barrier = None  # a threading.Barrier the first filter of each report waits on

    def _apply(self, tab, row):
        rows = self.tabs.setdefault(tab, {})
        k = row["key"]
        cur = dict(rows.get(k) or {})
        for f, v in row.items():
            if isinstance(v, dict) and len(v) == 1:
                (op, arg), = v.items()
                if op == "inc":
                    cur[f] = (cur.get(f) or 0) + arg
                    continue
                if op == "min":
                    cur[f] = arg if not cur.get(f) else min(cur[f], arg)
                    continue
                if op == "max":
                    cur[f] = arg if not cur.get(f) else max(cur[f], arg)
                    continue
            cur[f] = v
        rows[k] = cur
        self.versions[(tab, k)] = self.versions.get((tab, k), 0) + 1

    def mcp_tool(self, key, name, args, timeout=60):
        assert key == KEY
        if self.barrier is not None and name == "ledger_rows_filter" and not getattr(threading.current_thread(), "waited", False):
            threading.current_thread().waited = True
            self.barrier.wait(timeout=10)
        with self.lock:
            self.calls.append((name, json.loads(json.dumps(args))))
            if self.fail_next:
                f = self.fail_next.pop(0)
                if isinstance(f, Exception):
                    raise f
                return f
            tab = args.get("tab")
            if name == "ledger_rows_upsert":
                idem = args.get("idempotency_key")
                if idem and idem in self.seen_idem:
                    return False, self.seen_idem[idem]
                for r in args["rows"]:
                    self._apply(tab, r)
                out = {"saved": len(args["rows"])}
                if idem:
                    self.seen_idem[idem] = out
                return False, out
            if name == "ledger_rows_filter":
                if tab not in self.tabs:
                    return True, {"code": "not_found", "message": "no such tab"}
                rows = [dict(c) for c in self.tabs[tab].values() if self._match(c, args.get("where"))]
                for o in reversed(args.get("order") or []):
                    rows.sort(key=lambda c: str(c.get(o["column"]) or ""), reverse=o.get("dir") == "desc")
                start = int(args.get("cursor") or 0)
                page = rows[start:start + args.get("limit", 1000)]
                out = {"rows": page}
                if start + len(page) < len(rows):
                    out["cursor"] = str(start + len(page))
                return False, out
            if name == "ledger_rows_get":
                rows = self.tabs.get(tab, {})
                return False, {"rows": [dict(rows[k]) for k in args["keys"] if k in rows]}
            if name == "ledger_rows_delete":
                for k in args["keys"]:
                    self.tabs.get(tab, {}).pop(k, None)
                return False, {"deleted": len(args["keys"])}
            raise AssertionError(name)

    @staticmethod
    def _match(c, where):
        if not where:
            return True
        if "and" in where:
            return all(FakeDatastash._match(c, w) for w in where["and"])
        if "eq" in where:
            col, v = where["eq"]
            return c.get(col) == v
        raise AssertionError(where)

    def api_json(self, method, path, key, body=None, timeout=40):
        assert key == KEY
        with self.lock:
            self.calls.append((method + " " + path, body))
            if path.startswith(ds.ROWS_PATH + "/key?") and method == "GET":
                q = dict(urllib.parse.parse_qsl(path.split("?", 1)[1]))
                row = self.tabs.get(q["tab"], {}).get(q["key"])
                if row is None:
                    raise ds.ApiError(404, {"code": "not_found"})
                return {"cells": dict(row), "row_version": self.versions[(q["tab"], q["key"])]}
            if path == ds.ROWS_PATH and method == "POST":
                tab, row = body["tab"], body["row"]
                if body.get("if_version") is not None and self.versions.get((tab, row["key"]), 0) != body["if_version"]:
                    raise ds.ApiError(409, {"code": "version_conflict"})
                self._apply(tab, row)
                return {"row_ids": [1]}
            raise AssertionError(path)


def junit(cases, name="suite"):
    """cases: (classname, name, outcome, ms) with outcome pass | fail | error | skip."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>', f'<testsuites><testsuite name="{name}" tests="{len(cases)}">']
    for cls, n, outcome, ms in cases:
        out.append(f'<testcase classname="{cls}" name="{n}" time="{ms / 1000}">')
        if outcome == "fail":
            out.append(f'<failure message="expected 3 to be 4 at 0xdeadbeef {CANARY}" type="AssertionError">'
                       f'AssertionError: {CANARY} stack\n  at foo ({CANARY}.ts:1:2)</failure>')
        elif outcome == "error":
            out.append(f'<error type="TypeError: {CANARY}">{CANARY} first line\nsecond</error>')
        elif outcome == "skip":
            out.append('<skipped/>')
        out.append(f'<system-out>{CANARY} out</system-out><system-err>{CANARY} err</system-err></testcase>')
    out.append('</testsuite></testsuites>')
    return "\n".join(out)


class CiReport(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDatastash()
        self.dir = harness.tmpdir(self, prefix="ci-report-", dir=TMP)
        self.sleeps = []
        for name, new in (("mcp_tool", self.fake.mcp_tool), ("api_json", self.fake.api_json)):
            old = getattr(ds, name)
            setattr(ds, name, new)
            self.addCleanup(setattr, ds, name, old)
        old_sleep = ds.CiClient.__dict__["sleep"]
        ds.CiClient.sleep = staticmethod(lambda s: self.sleeps.append(s))
        self.addCleanup(setattr, ds.CiClient, "sleep", old_sleep)

    def file(self, text, name=None):
        if name:
            p = os.path.join(self.dir, name)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        else:  # a new name each call, also from two threads at once
            fd, p = tempfile.mkstemp(prefix="junit-", suffix=".xml", dir=self.dir)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        return p

    def report(self, cases, sha=SHA, branch="feature", job="test", now=None, local=False):
        return ds.ci_report([self.file(junit(cases))], "acme/widgets", sha, branch,
                            "https://github.com/acme/widgets/actions/runs/1", job, "main", KEY, WB, now=now, local=local)

    def row(self, tab, key):
        return self.fake.tabs.get(tab, {}).get(key)

    def trow(self, t):
        return self.row("Tests", f"acme/widgets / {t}")

    def fail_row(self, t, sha=SHA):
        return self.row("Test failures", f"acme/widgets / {sha} / {t}")

    # --- privacy

    def test_canary_never_leaves(self):
        p = self.file(junit([("test/a.test.ts", "adds", "fail", 12), ("test/a.test.ts", "subs", "error", 3),
                             ("test/b.test.ts", "muls", "pass", 5)]))
        s = ds.ci_report([p], "acme/widgets", SHA, "feature", "https://github.com/acme/widgets/actions/runs/1", "test",
                         "main", KEY, WB)
        self.assertEqual(s["failed"], 2)
        sent = json.dumps(self.fake.calls) + json.dumps(self.fake.tabs)
        self.assertNotIn(CANARY, sent)
        self.assertNotIn("stack", sent)
        r = self.fail_row("test/a.test.ts > adds")
        self.assertEqual(r["error_class"], "AssertionError")
        self.assertEqual(r["error_hash"], ds.ci_error_hash(f"expected 3 to be 4 at 0xdeadbeef {CANARY}"))
        self.assertEqual(self.fail_row("test/a.test.ts > subs")["error_class"], "TypeError")
        # Every sent field is on the allow-list.
        allowed = {"key", "repo", "sha", "test", "file", "branch", "run_url", "error_class", "error_hash", "fails", "passes",
                   "runs", "skips", "ms_sum", "last_ms", "last_outcome", "last_sha", "first_seen", "last_seen", "week",
                   "base_avg_ms", "main_outcome", "main_sha", "main_broke_at", "flaky_commits", "job", "set_hash", "tests",
                   "failed_count", "failed_ids", "failed_listed", "reports", "passed_hashes", "parts", "part"}
        for name, args in self.fake.calls:
            for r in (args or {}).get("rows") or []:
                self.assertLessEqual(set(r), allowed, name)

    def test_error_hash_groups_across_runs(self):
        a = ds.ci_error_hash("Timeout after 5003 ms waiting for 3f9a2c1d7e at 0x7ffd1234 id 6f1c2e3a-0b1c-4d2e-8f3a-1b2c3d4e5f60")
        b = ds.ci_error_hash("Timeout after 4999 ms waiting for 9e8d7c6b5a at 0x7ffd9999 id 00000000-1111-2222-3333-444444444444")
        self.assertEqual(a, b)
        self.assertNotEqual(a, ds.ci_error_hash("expected true to be false"))
        self.assertEqual(ds.ci_error_class("AssertionError: secret words", "failure"), "AssertionError")
        self.assertEqual(ds.ci_error_class("", "error"), "Error")
        self.assertEqual(ds.ci_error_class("  <bad>", "failure"), "Failure")

    def test_cli_output_has_no_message_text(self):
        p = self.file(junit([("t", "one", "fail", 1)]))
        r = self.run_cli(p, "--repo", "acme/widgets", "--sha", SHA, "--branch", "b", "--run-url", "https://x.example/1")
        self.assertEqual(r.returncode, 0)
        self.assertNotIn(CANARY, r.stdout + r.stderr)

    # --- XML hardening

    def test_billion_laughs_refused(self):
        lol = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
               + "".join(f'<!ENTITY lol{i} "{("&lol%s;" % ("" if i == 1 else i - 1)) * 10}">' for i in range(1, 10))
               + ']><testsuite><testcase classname="c" name="&lol9;"/></testsuite>')
        with self.assertRaises(ds.CiProblem) as cm:
            ds.ci_parse([self.file(lol)])
        self.assertIn("DOCTYPE", str(cm.exception))
        self.assertEqual(self.fake.calls, [])

    def test_external_entity_refused(self):
        secret = self.file("TOP SECRET FILE", "secret.txt")
        xxe = (f'<?xml version="1.0"?><!DOCTYPE t [<!ENTITY x SYSTEM "file://{secret}">]>'
               '<testsuite><testcase classname="c" name="&x;"/></testsuite>')
        with self.assertRaises(ds.CiProblem):
            ds.ci_parse([self.file(xxe)])
        with self.assertRaises(ds.CiProblem):  # an undeclared entity is an error, not text
            ds.ci_parse([self.file('<testsuite><testcase classname="c" name="&x;"/></testsuite>')])
        with self.assertRaises(ds.CiProblem):
            ds.ci_parse([self.file('<testsuite><testcase')])

    def test_parse_shapes(self):
        xml = ('<testsuites><testsuite name="s" file="test/x.test.ts"><testsuite name="inner">'
               '<testcase classname="test/x.test.ts" name="a &gt; b" time="0.25"/>'
               '<testcase classname="test/x.test.ts" name="skipped one" time="0"><skipped message="todo"/></testcase>'
               '</testsuite></testsuite>'
               '<testsuite name="py"><testcase classname="tests.test_m.TestA" name="test_b" file="tests/test_m.py" time="1.5">'
               '<failure>\n\nfirst real line\nsecond</failure></testcase></testsuite></testsuites>')
        t = ds.ci_parse([self.file(xml)])
        self.assertEqual(t["test/x.test.ts > a > b"], {"file": "test/x.test.ts", "runs": 1, "passes": 1, "fails": 0,
                                                       "skips": 0, "ms": 250})
        self.assertEqual((t["test/x.test.ts > skipped one"]["skips"], t["test/x.test.ts > skipped one"]["ms"]), (1, 0))
        py = t["tests/test_m.py > tests.test_m.TestA > test_b"]
        self.assertEqual((py["file"], py["fails"], py["ms"], py["error_class"]), ("tests/test_m.py", 1, 1500, "Failure"))
        self.assertEqual(py["error_hash"], ds.ci_error_hash("first real line"))

    # --- rows

    def test_rows_folded(self):
        self.report([("t", "a", "pass", 10), ("t", "b", "fail", 30), ("t", "c", "skip", 0)])
        self.report([("t", "a", "pass", 20), ("t", "b", "pass", 10), ("t", "c", "skip", 0)], sha="b" * 40)
        a = self.trow("t > a")
        self.assertEqual((a["runs"], a["fails"], a["ms_sum"], a["last_ms"], a["last_outcome"], a["last_sha"]),
                         (2, 0, 30, 20, "pass", "b" * 40))
        c = self.trow("t > c")
        self.assertEqual((c["runs"], c["skips"], c["last_outcome"]), (0, 2, "skip"))
        self.assertNotIn("last_ms", c)
        self.assertIsNone(self.fail_row("t > a"))
        self.assertEqual(self.fail_row("t > b")["fails"], 1)
        self.assertNotIn("flaky_commits", self.trow("t > b"))  # a pass on another commit is not a flake
        weeks = list(self.fake.tabs["Test weeks"].values())
        self.assertEqual(len(weeks), 1)
        self.assertEqual((weeks[0]["runs"], weeks[0]["fails"], weeks[0]["ms_sum"]), (4, 1, 70))

    def test_week_base_from_four_weeks_back(self):
        now = time.time()
        old = now - 28 * 86400
        self.report([("t", "a", "pass", 100)], sha="c" * 40, now=old)
        self.report([("t", "a", "pass", 300)], now=now)
        row = self.row("Test weeks", f"acme/widgets / t / {ds.ci_iso_week(now)}")
        self.assertEqual(row["base_avg_ms"], 100)

    def test_main_broke(self):
        self.report([("t", "a", "pass", 1)], sha="d" * 40, branch="main")
        self.assertEqual(self.trow("t > a")["main_outcome"], "pass")
        self.report([("t", "a", "fail", 1)], branch="main")
        r = self.trow("t > a")
        self.assertEqual((r["main_outcome"], r["main_sha"], r["main_broke_at"]), ("fail", SHA, SHA))
        self.report([("t", "a", "fail", 1)], sha="e" * 40, branch="main")
        self.assertEqual(self.trow("t > a")["main_broke_at"], SHA)  # still red: the first red commit stays
        self.report([("t", "a", "fail", 1)], sha="f" * 40, branch="feature")
        self.assertEqual(self.trow("t > a")["main_sha"], "e" * 40)  # another branch never moves main's state

    def test_pages_and_idempotency(self):
        cases = [("t", f"n{i}", "pass", 1) for i in range(2500)]
        self.report(cases)
        pages = [a for n, a in self.fake.calls if n == "ledger_rows_upsert" and a["tab"] == "Tests"]
        self.assertEqual([len(a["rows"]) for a in pages], [1000, 1000, 500])
        self.assertEqual(len({a["idempotency_key"] for a in pages}), 3)
        self.assertEqual(len(self.fake.tabs["Tests"]), 2500)

    def test_429_and_busy_waited_out(self):
        self.fake.fail_next = [ds.ApiError(429, {}), (True, {"code": "busy", "message": "another writer"}),
                               (True, {"code": "agent_slowed_down", "retry_after_seconds": 7})]
        self.fake.fail_next[0].retry_after = 11
        self.report([("t", "a", "fail", 1)])
        self.assertEqual(self.sleeps[:3], [11.0, 3.0, 7.0])
        self.assertEqual(self.fail_row("t > a")["fails"], 1)

    def test_retry_after_lost_answer_counts_once(self):
        orig = self.fake.mcp_tool
        state = {"n": 0}

        def flaky_network(key, name, args, timeout=60):
            out = orig(key, name, args, timeout)
            if name == "ledger_rows_upsert" and args["tab"] == "Tests" and state["n"] == 0:
                state["n"] = 1
                raise OSError("connection reset")  # written, but the answer was lost
            return out
        ds.mcp_tool = flaky_network
        self.report([("t", "a", "pass", 5)])
        self.assertEqual(self.trow("t > a")["runs"], 1)

    def test_write_to_missing_workbook_is_a_problem(self):
        self.fake.fail_next = [(True, {"code": "not_found", "message": "No workbook with that id"})]
        with self.assertRaises(ds.CiProblem) as cm:
            self.report([("t", "a", "fail", 1)])
        self.assertIn("No workbook", str(cm.exception))

    def test_old_runs_rows_removed(self):
        now = time.time()
        self.report([("t", "a", "pass", 1)], sha="9" * 40, now=now - 20 * 86400)
        self.assertEqual(len(self.fake.tabs["Test runs"]), 1)
        self.report([("t", "a", "pass", 1)], now=now)
        self.assertEqual([r["sha"] for r in self.fake.tabs["Test runs"].values()], [SHA])

    # --- flaky, order-independent

    def assert_flaky_once(self, t="t > a"):
        self.assertEqual(self.trow(t).get("flaky_commits"), 1)
        self.assertGreaterEqual(self.fail_row(t)["passes"], 1)

    def test_flaky_pass_after_failure(self):
        self.report([("t", "a", "fail", 1), ("t", "b", "pass", 1)], job="j1")
        self.report([("t", "a", "pass", 1), ("t", "b", "pass", 1)], job="j1")
        self.assert_flaky_once()

    def test_flaky_failure_after_pass(self):
        # The card A review case: a parallel job reports the pass before the failure row exists.
        self.report([("t", "a", "pass", 1), ("t", "b", "pass", 1)], job="linux")
        self.report([("t", "a", "fail", 1), ("t", "b", "pass", 1)], job="mac")
        self.assert_flaky_once()
        self.assertIsNone(self.trow("t > b").get("flaky_commits"))

    def test_flaky_both_failing_different_tests(self):
        self.report([("t", "a", "fail", 1), ("t", "b", "pass", 1)])
        self.report([("t", "a", "pass", 1), ("t", "b", "fail", 1)])
        self.assert_flaky_once("t > a")
        self.assert_flaky_once("t > b")

    def test_flaky_reports_at_once_counted_once(self):
        # Both reports write, then both read (a barrier at the first read): each sees the other; one adds flaky_commits.
        self.fake.barrier = threading.Barrier(2)
        errs = []

        def run(cases, job):
            try:
                self.report(cases, job=job)
            except Exception as e:  # noqa: BLE001
                errs.append(e)
        a = threading.Thread(target=run, args=([("t", "a", "pass", 1), ("t", "b", "pass", 1)], "linux"))
        b = threading.Thread(target=run, args=([("t", "a", "fail", 1), ("t", "b", "pass", 1)], "mac"))
        a.start(), b.start()
        a.join(20), b.join(20)
        self.assertEqual(errs, [])
        self.assert_flaky_once()

    def test_three_reports_any_order_counted_once(self):
        runs = [[("t", "a", "pass", 1)], [("t", "a", "fail", 1)], [("t", "a", "pass", 1)]]
        for order in ((0, 1, 2), (1, 0, 2), (0, 2, 1)):
            self.setUp()
            for i in order:
                self.report(runs[i], job=f"j{i}")
            self.assert_flaky_once()

    def test_flaky_legs_with_different_test_sets_either_order(self):
        # Review cycle 2 item 1: a matrix leg that skips or lacks another test; pass first or failure first, one rule.
        linux = [("t", "a", "pass", 1), ("t", "c", "pass", 1)]
        mac = [("t", "a", "fail", 1), ("t", "b", "pass", 1)]
        for order in ((linux, mac), (mac, linux)):
            self.setUp()
            for cases in order:
                self.report(cases, job="leg")
            self.assert_flaky_once()
            self.assertIsNone(self.trow("t > b").get("flaky_commits"))
            self.assertIsNone(self.trow("t > c").get("flaky_commits"))

    def test_flaky_pass_hashes_over_several_runs_rows(self):
        # A big leg's passes span several Test runs rows; a failure after it still finds each one.
        old = ds.CI_IDS_CHARS
        ds.CI_IDS_CHARS = (ds.CI_PASS_HASH + 1) * 2  # two hashes a row
        self.addCleanup(setattr, ds, "CI_IDS_CHARS", old)
        names = [f"n{i}" for i in range(5)]
        self.report([("t", n, "pass", 1) for n in names] + [("t", "only", "pass", 1)], job="big")
        self.assertEqual(len(self.fake.tabs["Test runs"]), 3)  # six passes, two hashes a row
        self.report([("t", n, "fail", 1) for n in names], job="small")
        for n in names:
            self.assert_flaky_once(f"t > {n}")

    def test_local_run_never_touches_flaky_bookkeeping(self):
        # Review cycle 2 item 2: a local failure on uncommitted edits plus a CI pass on HEAD is not a flake.
        s = self.report([("t", "a", "fail", 1), ("t", "b", "pass", 1)], local=True)
        self.assertEqual((s["failed"], s["flaky"]), (1, []))
        self.assertNotIn("Test failures", self.fake.tabs)
        self.assertNotIn("Test runs", self.fake.tabs)
        self.assertEqual(self.trow("t > a")["last_outcome"], "fail")  # the Tests row is still kept
        self.report([("t", "a", "pass", 1), ("t", "b", "pass", 1)], job="ci")
        self.report([("t", "a", "fail", 1), ("t", "a", "pass", 1)], local=True)  # a local retry: no flaky either
        self.assertIsNone(self.trow("t > a").get("flaky_commits"))
        self.assertIsNone(self.fail_row("t > a"))
        self.report([("t", "b", "fail", 1)], branch="main", local=True)
        self.assertNotIn("main_outcome", self.trow("t > b"))  # and never main's state
        self.assertEqual(list(self.fake.tabs.get("Test failures", {})), [])  # the CI pass made none; local none either
        self.assertEqual(len(self.fake.tabs["Test runs"]), 1)  # only the CI report's

    def test_cli_local_flag_reaches_the_report(self):
        got = {}
        old = ds.ci_report
        ds.ci_report = lambda *a, **k: got.update(k) or {"tests": 1, "files": 1, "failed": 0, "flaky": [], "broke": []}
        self.addCleanup(setattr, ds, "ci_report", old)
        env = {k: os.environ.get(k) for k in ("DATASTASH_AGENT_KEY", "DATASTASH_WORKBOOK_ID")}
        os.environ["DATASTASH_AGENT_KEY"], os.environ["DATASTASH_WORKBOOK_ID"] = KEY, WB
        try:
            from io import StringIO
            from contextlib import redirect_stdout
            with redirect_stdout(StringIO()):
                ds.cmd_ci_report([self.file(junit([("t", "a", "pass", 1)])), "--local", "--repo", "a/b", "--sha", SHA,
                                  "--branch", "x"])
        finally:
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertIs(got.get("local"), True)

    def test_shards_are_not_flaky(self):
        self.report([("t", "a", "pass", 1)], job="shard1")
        self.report([("t", "b", "fail", 1)], job="shard2")
        self.assertIsNone(self.trow("t > b").get("flaky_commits"))
        self.assertIsNone(self.fail_row("t > b").get("passes"))

    def test_both_failing_is_not_flaky(self):
        self.report([("t", "a", "fail", 1)], job="linux")
        self.report([("t", "a", "fail", 1)], job="mac")
        self.assertIsNone(self.trow("t > a").get("flaky_commits"))
        self.assertEqual(self.fail_row("t > a")["fails"], 2)

    def test_skipped_elsewhere_is_not_flaky(self):
        self.report([("t", "a", "skip", 0), ("t", "b", "pass", 1)], job="linux")
        self.report([("t", "a", "fail", 1), ("t", "b", "pass", 1)], job="mac")
        self.assertIsNone(self.trow("t > a").get("flaky_commits"))

    def test_retry_inside_one_report(self):
        self.report([("t", "a", "fail", 1), ("t", "a", "pass", 1)])
        self.assert_flaky_once()
        self.assertEqual(self.trow("t > a")["runs"], 2)

    def test_other_commit_never_flaky(self):
        self.report([("t", "a", "pass", 1)], sha="1" * 40)
        self.report([("t", "a", "fail", 1)], sha="2" * 40)
        self.assertIsNone(self.trow("t > a").get("flaky_commits"))

    # --- the command never fails a build

    def run_cli(self, *args, key=KEY, wb=WB, wait=None):
        env = dict(os.environ, DATASTASH_NO_NETWORK="1", DATASTASH_CI_RETRY_SECONDS="0", DATASTASH_AGENT_KEY=key,
                   DATASTASH_WORKBOOK_ID=wb)
        if wait is not None:
            env["DATASTASH_CI_WAIT_SECONDS"] = wait
        return subprocess.run([sys.executable, "-I", BIN, "ci-report"] + list(args), capture_output=True, text=True,
                              env=env, timeout=60)

    def test_cli_always_exits_zero_with_one_line(self):
        p = self.file(junit([("t", "a", "pass", 1)]))
        good = ["--repo", "acme/widgets", "--sha", SHA, "--branch", "main", "--run-url", "https://github.com/a/b/actions/runs/1"]
        for args, key, wb, want in (
                ([p] + good, KEY, WB, "can't be reached"),
                ([p] + good, "", WB, "DATASTASH_AGENT_KEY"),
                ([p] + good, KEY, "", "DATASTASH_WORKBOOK_ID"),
                ([p, "--repo", "acme/widgets"], KEY, WB, "--sha is missing"),
                ([p, "--bogus"] + good, KEY, WB, "unknown option"),
                ([os.path.join(self.dir, "none.xml")] + good, KEY, WB, "can't be read"),
                ([p] + good[:-1] + ["https://u:pw@github.com/x"], KEY, WB, "no user or password"),
                ([p] + good[:2] + ["--sha", "not-a-sha"] + good[4:], KEY, WB, "hex id")):
            r = self.run_cli(*args, key=key, wb=wb)
            self.assertEqual(r.returncode, 0, args)
            lines = (r.stdout + r.stderr).strip().splitlines()
            self.assertEqual(len(lines), 1, lines)
            self.assertIn("not sent", lines[0])
            self.assertIn(want, lines[0])
            self.assertNotIn(KEY, lines[0])

    def test_bad_wait_setting_falls_back_and_exits_zero(self):
        # Review cycle 2 item 3: a bad DATASTASH_CI_WAIT_SECONDS no longer stops the script at import.
        for bad in ("abc", "nan", "inf", "-5", "0", ""):
            os.environ["DATASTASH_CI_WAIT_SECONDS"] = bad
            try:
                self.assertEqual(ds._ci_env_seconds("DATASTASH_CI_WAIT_SECONDS", 900.0), 900.0, bad)
            finally:
                os.environ.pop("DATASTASH_CI_WAIT_SECONDS", None)
            p = self.file(junit([("t", "a", "pass", 1)]))
            r = self.run_cli(p, "--repo", "acme/widgets", "--sha", SHA, "--branch", "b", "--run-url", "https://x.example/1",
                             wait=bad)
            self.assertEqual(r.returncode, 0, bad)
            lines = (r.stdout + r.stderr).strip().splitlines()
            self.assertEqual(len(lines), 1, (bad, lines))
            self.assertIn("can't be reached", lines[0])
        os.environ["DATASTASH_CI_WAIT_SECONDS"] = "12.5"
        try:
            self.assertEqual(ds._ci_env_seconds("DATASTASH_CI_WAIT_SECONDS", 900.0), 12.5)
        finally:
            os.environ.pop("DATASTASH_CI_WAIT_SECONDS", None)

    def test_bad_retry_setting_falls_back_and_exits_zero(self):
        # Release 1.0.0: a bad DATASTASH_CI_RETRY_SECONDS falls back to the default like the wait setting (0 stays 0).
        for bad in ("abc", "nan", "inf", "-5", ""):
            os.environ["DATASTASH_CI_RETRY_SECONDS"] = bad
            try:
                self.assertEqual(ds._ci_env_seconds("DATASTASH_CI_RETRY_SECONDS", 2.0, zero_ok=True), 2.0, bad)
            finally:
                os.environ.pop("DATASTASH_CI_RETRY_SECONDS", None)
        for bad in ("abc", "-5"):  # the script itself (it used to stop at import); the default waits run here
            p = self.file(junit([("t", "a", "pass", 1)]))
            env = dict(os.environ, DATASTASH_NO_NETWORK="1", DATASTASH_CI_RETRY_SECONDS=bad, DATASTASH_AGENT_KEY=KEY,
                       DATASTASH_WORKBOOK_ID=WB)
            r = subprocess.run([sys.executable, "-I", BIN, "ci-report", p, "--repo", "acme/widgets", "--sha", SHA,
                                "--branch", "b", "--run-url", "https://x.example/1"], capture_output=True, text=True,
                               env=env, timeout=60)
            self.assertEqual(r.returncode, 0, bad)
            lines = (r.stdout + r.stderr).strip().splitlines()
            self.assertEqual(len(lines), 1, (bad, lines))
            self.assertIn("can't be reached", lines[0])
        for good, want in (("0", 0.0), ("0.5", 0.5)):
            os.environ["DATASTASH_CI_RETRY_SECONDS"] = good
            try:
                self.assertEqual(ds._ci_env_seconds("DATASTASH_CI_RETRY_SECONDS", 2.0, zero_ok=True), want)
            finally:
                os.environ.pop("DATASTASH_CI_RETRY_SECONDS", None)

    def test_hung_server_stops_within_the_short_total(self):
        # Review cycle 2 item 3: each call is given at most CI_CALL_SECONDS and unanswered calls CI_HANG_SECONDS in all,
        # however long the brake wait (CI_WAIT_SECONDS) is.
        for name, v in (("CI_CALL_SECONDS", 0.2), ("CI_HANG_SECONDS", 0.5), ("CI_RETRIES", 50)):
            old = getattr(ds, name)
            setattr(ds, name, v)
            self.addCleanup(setattr, ds, name, old)
        timeouts = []

        def hung(key, name, args, timeout=60):
            timeouts.append(timeout)
            time.sleep(timeout)
            raise socket.timeout("timed out")
        ds.mcp_tool = hung
        began = time.time()
        with self.assertRaises(ds.CiProblem) as cm:
            self.report([("t", "a", "fail", 1)])
        self.assertLess(time.time() - began, 2)
        self.assertIn("did not answer", str(cm.exception))
        self.assertTrue(all(t <= 0.2 for t in timeouts), timeouts)
        self.assertLessEqual(sum(timeouts), 0.5 + 0.1 + 1e-6)
        self.assertGreaterEqual(len(timeouts), 3)

    def test_calls_are_given_the_time_left_under_the_deadline(self):
        seen = []
        orig = self.fake.mcp_tool
        ds.mcp_tool = lambda key, name, args, timeout=60: seen.append(timeout) or orig(key, name, args, timeout)
        self.report([("t", "a", "pass", 1)])
        self.assertTrue(seen)
        self.assertTrue(all(0 < t <= ds.CI_CALL_SECONDS for t in seen), seen)

    def test_cli_internal_error_exits_zero(self):
        old = ds.ci_report
        ds.ci_report = lambda *a, **k: 1 / 0
        self.addCleanup(setattr, ds, "ci_report", old)
        old_key = os.environ.get("DATASTASH_AGENT_KEY")
        os.environ["DATASTASH_AGENT_KEY"], os.environ["DATASTASH_WORKBOOK_ID"] = KEY, WB
        try:
            from io import StringIO
            from contextlib import redirect_stdout
            buf = StringIO()
            with redirect_stdout(buf):
                rc = ds.cmd_ci_report([self.file(junit([("t", "a", "pass", 1)])), "--repo", "a/b", "--sha", SHA,
                                       "--branch", "x", "--run-url", ""])
        finally:
            os.environ.pop("DATASTASH_WORKBOOK_ID", None)
            if old_key is None:
                os.environ.pop("DATASTASH_AGENT_KEY", None)
            else:
                os.environ["DATASTASH_AGENT_KEY"] = old_key
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "datastash ci-report: not sent: unexpected ZeroDivisionError")


if __name__ == "__main__":
    unittest.main()
