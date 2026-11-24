#!/usr/bin/env python3
"""Offline tests for XLI-378 D (plugin 1.0.6): `datastash eval-report`, eval results into the "evals" preset tabs.
- Privacy: a canary in prompts, vars, expected answers and outputs never reaches datastash or the output; only names,
  pass, scores, counts, times, tokens, the tool's cost and a sha256 of the output do (none with --no-output-hash).
- JSONL, promptfoo's JSON output and an Inspect eval log are read; runs of one case fold into one row (inc, whole
  numbers: score_milli and cost_micros).
- --vs PREV copies each case's passed at PREV as prev_passed (and names prev_version on the Eval versions row); a case
  that was not there gets none. No rate is computed here: the engine's formula columns do that.
- The command always exits 0 with one line, also for bad arguments, a missing key and no network.
No network, no key; the fake datastash of test_ci_report, under test/harness.py.
Run: python3 -I test/test_eval_report.py"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import ds, BIN, TMP  # noqa: E402  (the same harness and plugin copy)
from test_ci_report import FakeDatastash, KEY, WB  # noqa: E402
import harness  # noqa: E402

CANARY = "EVAL_CANARY_4c1d"
ALLOWED = {"key", "suite", "case", "model", "version", "version_key", "runs", "passes", "score_milli", "last_score", "passed",
           "prev_passed", "ms_sum", "tokens", "cost_micros", "output_hash", "first_seen", "last_seen", "prev_version"}


class EvalReport(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDatastash()
        self.dir = harness.tmpdir(self, prefix="eval-report-", dir=TMP)
        for name, new in (("mcp_tool", self.fake.mcp_tool), ("api_json", self.fake.api_json)):
            old = getattr(ds, name)
            setattr(ds, name, new)
            self.addCleanup(setattr, ds, name, old)
        old_sleep = ds.CiClient.__dict__["sleep"]
        ds.CiClient.sleep = staticmethod(lambda s: None)
        self.addCleanup(setattr, ds.CiClient, "sleep", old_sleep)

    def file(self, text, suffix=".jsonl"):
        fd, p = tempfile.mkstemp(prefix="evals-", suffix=suffix, dir=self.dir)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        return p

    def jsonl(self, *recs):
        return self.file("\n".join(json.dumps(r) for r in recs) + "\n")

    def erow(self, case, version, model="model-a", suite="checkout"):
        return self.fake.tabs.get("Evals", {}).get(f"{suite} / {case} / {model} / {version}")

    def sent_rows(self):
        return [r for name, args in self.fake.calls if name == "ledger_rows_upsert" for r in args["rows"]]

    def test_jsonl_folds_and_sends_only_allowed_fields(self):
        p = self.jsonl(
            {"case": "a", "passed": True, "score": 1, "ms": 200, "tokens": 50, "cost": 0.0012, "prompt": CANARY, "output": f"x {CANARY}",
             "vars": {"q": CANARY}, "expected": CANARY},
            {"case": "a", "passed": True, "score": 0.8, "ms": 400, "tokens": 50, "output": "y"},
            {"case": "b", "passed": False, "score": 0.25, "latency_ms": 100},
            {"case": "c", "score": 1.0})  # no pass field: passes at the default --pass-at 1.0
        s = ds.eval_report([p], "checkout", "v11", "model-a", "", KEY, WB)
        self.assertEqual((s["cases"], s["runs"], s["passed"], s["compared"]), (3, 4, 2, 0))
        a = self.erow("a", "v11")
        self.assertEqual((a["runs"], a["passes"], a["score_milli"], a["ms_sum"], a["tokens"], a["cost_micros"]), (2, 2, 1800, 600, 100, 1200))
        self.assertEqual((a["passed"], a["last_score"], a["version_key"]), (True, 0.8, "checkout / model-a / v11"))
        self.assertEqual(a["output_hash"], ds.hashlib.sha256(b"y").hexdigest())
        self.assertEqual((self.erow("b", "v11")["passed"], self.erow("b", "v11")["score_milli"], self.erow("b", "v11")["ms_sum"]), (False, 250, 100))
        self.assertTrue(self.erow("c", "v11")["passed"])
        self.assertNotIn("prev_passed", a)
        v = self.fake.tabs["Eval versions"]["checkout / model-a / v11"]
        self.assertEqual((v["suite"], v["model"], v["version"]), ("checkout", "model-a", "v11"))
        self.assertNotIn("prev_version", v)
        sent = json.dumps(self.fake.calls)
        self.assertNotIn(CANARY, sent)
        for r in self.sent_rows():
            self.assertLessEqual(set(r), ALLOWED)
            for f in ("runs", "passes", "score_milli", "ms_sum", "tokens", "cost_micros"):
                if f in r:
                    self.assertIsInstance(r[f]["inc"], int, f)  # inc takes whole numbers only

    def test_vs_copies_prev_passed(self):
        ds.eval_report([self.jsonl({"case": "a", "passed": True}, {"case": "b", "passed": True})], "checkout", "v11", "model-a", "", KEY, WB)
        s = ds.eval_report([self.jsonl({"case": "a", "passed": True}, {"case": "b", "passed": False}, {"case": "new", "passed": True})],
                           "checkout", "v12", "model-a", "v11", KEY, WB)
        self.assertEqual(s["compared"], 2)
        self.assertEqual(self.erow("b", "v12")["prev_passed"], True)
        self.assertEqual(self.erow("b", "v12")["passed"], False)
        self.assertEqual(self.erow("a", "v12")["prev_passed"], True)
        self.assertNotIn("prev_passed", self.erow("new", "v12"))
        self.assertEqual(self.fake.tabs["Eval versions"]["checkout / model-a / v12"]["prev_version"], "v11")
        # The read came before the write, and asked only for the same cases at v11.
        names = [n for n, _a in self.fake.calls]
        gets = [a for n, a in self.fake.calls if n == "ledger_rows_get"]
        self.assertLess(names.index("ledger_rows_get"), max(i for i, n in enumerate(names) if n == "ledger_rows_upsert"))
        self.assertEqual(sorted(gets[-1]["keys"]), ["checkout / a / model-a / v11", "checkout / b / model-a / v11", "checkout / new / model-a / v11"])

    def test_promptfoo_output(self):
        doc = {"results": {"results": [
            {"testIdx": 0, "testCase": {"description": "refund", "vars": {"q": CANARY}}, "prompt": {"raw": CANARY, "label": "p1"},
             "provider": {"id": "openai:gpt-x", "label": "gpt-x"}, "success": True, "score": 1, "latencyMs": 321,
             "cost": 0.000123, "response": {"output": CANARY, "tokenUsage": {"total": 77}}, "gradingResult": {"reason": CANARY}},
            {"testIdx": 1, "testCase": {"vars": {"q": CANARY}}, "provider": "anthropic:claude-y", "success": False, "score": 0,
             "latencyMs": 10, "response": {"output": "no"}}]}}
        ds.eval_report([self.file(json.dumps(doc), ".json")], "support", "v3", "", "", KEY, WB)
        r = self.erow("refund", "v3", model="gpt-x", suite="support")
        self.assertEqual((r["passed"], r["ms_sum"], r["tokens"], r["cost_micros"], r["score_milli"]), (True, 321, 77, 123, 1000))
        self.assertFalse(self.erow("test-1", "v3", model="anthropic:claude-y", suite="support")["passed"])
        self.assertEqual(sorted(self.fake.tabs["Eval versions"]), ["support / anthropic:claude-y / v3", "support / gpt-x / v3"])
        self.assertNotIn(CANARY, json.dumps(self.fake.calls))

    def test_inspect_log(self):
        doc = {"eval": {"task": "math", "model": "provider/model-z"}, "samples": [
            {"id": 1, "input": CANARY, "target": CANARY, "scores": {"match": {"value": "C", "explanation": CANARY}},
             "total_time": 1.5, "model_usage": {"provider/model-z": {"total_tokens": 40}}, "output": {"completion": CANARY}},
            {"id": 2, "input": CANARY, "scores": {"match": {"value": "I"}}, "total_time": 0.5},
            {"id": 3, "scores": {"match": {"value": 0.75}}}]}
        ds.eval_report([self.file(json.dumps(doc), ".json")], "math", "r1", "", "", KEY, WB, pass_at=0.7)
        one = self.erow("1", "r1", model="provider/model-z", suite="math")
        self.assertEqual((one["passed"], one["ms_sum"], one["tokens"], one["score_milli"]), (True, 1500, 40, 1000))
        self.assertFalse(self.erow("2", "r1", model="provider/model-z", suite="math")["passed"])
        self.assertTrue(self.erow("3", "r1", model="provider/model-z", suite="math")["passed"])  # 0.75 >= --pass-at 0.7
        self.assertNotIn(CANARY, json.dumps(self.fake.calls))

    def test_no_output_hash_and_problems(self):
        ds.eval_report([self.jsonl({"case": "a", "passed": True, "output": "secret-ish"})], "s", "v1", "m", "", KEY, WB, output_hash=False)
        self.assertNotIn("output_hash", self.erow("a", "v1", model="m", suite="s"))
        with self.assertRaises(ds.CiProblem) as cm:
            ds.eval_report([self.jsonl({"case": "a", "passed": True})], "s", "v1", "", "", KEY, WB)
        self.assertIn("--model", str(cm.exception))
        with self.assertRaises(ds.CiProblem):
            ds.eval_report([self.file("not json\n")], "s", "v1", "m", "", KEY, WB)
        with self.assertRaises(ds.CiProblem):
            ds.eval_report([self.jsonl({"passed": True})], "s", "v1", "m", "", KEY, WB)

    def run_cli(self, *args, key=KEY, wb=WB):
        env = dict(os.environ, DATASTASH_NO_NETWORK="1", DATASTASH_CI_RETRY_SECONDS="0", DATASTASH_AGENT_KEY=key,
                   DATASTASH_WORKBOOK_ID=wb)
        return subprocess.run([sys.executable, "-I", BIN, "eval-report"] + list(args), capture_output=True, text=True,
                              env=env, timeout=60)

    def test_cli_always_exits_zero_with_one_line(self):
        p = self.jsonl({"case": "a", "passed": True, "output": CANARY})
        good = ["--suite", "s", "--version", "v1", "--model", "m"]
        for args, key, wb, want in (
                ([p] + good, KEY, WB, "can't be reached"),
                ([p] + good, "", WB, "DATASTASH_AGENT_KEY"),
                ([p] + good, KEY, "nope", "DATASTASH_WORKBOOK_ID"),
                ([p, "--version", "v1"], KEY, WB, "--suite is missing"),
                ([p] + good + ["--pass-at", "x"], KEY, WB, "--pass-at"),
                ([p] + good + ["--bogus"], KEY, WB, "unknown option"),
                (good, KEY, WB, "name the results file"),
                ([os.path.join(self.dir, "missing.jsonl")] + good, KEY, WB, "can't be read")):
            r = self.run_cli(*args, key=key, wb=wb)
            self.assertEqual(r.returncode, 0, args)
            lines = r.stdout.strip().splitlines()
            self.assertEqual(len(lines), 1, r.stdout)
            self.assertIn("not sent", lines[0])
            self.assertIn(want, lines[0], args)
            self.assertNotIn(CANARY, r.stdout + r.stderr)
        h = self.run_cli("--help")
        self.assertEqual(h.returncode, 0)
        self.assertIn("usage: datastash eval-report", h.stdout)


if __name__ == "__main__":
    unittest.main()
