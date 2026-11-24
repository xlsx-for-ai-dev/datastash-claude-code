#!/usr/bin/env python3
"""Offline tests for XLI-378 G (plugin 1.0.8): `datastash xlsx-test`, spreadsheet output tests into the "outputs" preset.
- The produced file goes to the engine only (chunked cache upload under one random handle, then finalize); its bytes never
  reach datastash or the output. The engine key goes only to the engine, the agent key only to datastash.
- One comparison formula per expected cell (number with tolerance, text with EXACT, a date with DATE), plus the cell
  itself for last_actual; the engine's TRUE passes, FALSE fails, anything else is "not known" and fails too. This command
  compares nothing itself (the fake engine below answers the formulas).
- The engine's stale_cached_value findings fail the run; formulas go in calls of at most 100.
- --record copies the engine's values as expected (an error cell is skipped; a row's own tolerance is kept).
- Exit 0 all passed, 1 a value moved / not known / stale, 2 the test could not run; one line per problem, no traceback.
No network, no key; the fake datastash of test_ci_report, under test/harness.py.
Run: python3 -I test/test_xlsx_test.py"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import ds, BIN, TMP  # noqa: E402  (the same harness and plugin copy)
from test_ci_report import FakeDatastash, KEY, WB  # noqa: E402
import harness  # noqa: E402

ENGINE_KEY = "xfa_enginekeyenginekey"
CANARY = b"XLSX_FILE_CANARY_7e2a"
SHA = "a1b2c3d4e5f6"
REF_RE = r"'((?:[^']|'')*)'!([A-Z]{1,3}[0-9]+)"


class FakeEngine:
    """The engine's cache and the two tools, answering the formula shapes xlsx-test sends from a dict of cell values."""

    def __init__(self, cells):
        self.cells = cells  # {("Summary", "B7"): {"type": "number", "value": 1234.5}}
        self.chunks, self.finalized, self.calls, self.findings = {}, set(), [], []
        self.fail_next = []

    def value(self, sheet, cell):
        return self.cells.get((sheet.replace("''", "'"), cell), {"type": "blank", "value": None})

    def answer(self, f):
        m = re.fullmatch("=" + REF_RE, f)
        if m:
            return self.value(*m.groups())
        m = re.fullmatch(r"=ABS\(" + REF_RE + r"-(?:\((-?[0-9.e+-]+)\)|DATE\((\d+),(\d+),(\d+)\))\)<=([0-9.e+-]+)", f)
        if m:
            v = self.value(m.group(1), m.group(2))
            if v["type"] != "number":
                return {"type": "error", "value": "#VALUE!"}
            want = float(m.group(3)) if m.group(3) is not None else ds_date_serial(*map(int, m.group(4, 5, 6)))
            return {"type": "boolean", "value": abs(v["value"] - want) <= float(m.group(7))}
        m = re.fullmatch(r"=EXACT\(" + REF_RE + r",\"((?:[^\"]|\"\")*)\"\)", f)
        if m:
            v = self.value(m.group(1), m.group(2))
            text = "" if v["value"] is None else (("TRUE" if v["value"] else "FALSE") if v["type"] == "boolean" else str(v["value"]))
            return {"type": "boolean", "value": text == m.group(3).replace("\"\"", "\"")}
        raise AssertionError("formula shape not expected: " + f)

    def __call__(self, path, body, engine_key, deadline, what):
        assert engine_key == ENGINE_KEY
        self.calls.append((path, json.loads(json.dumps(body))))
        if self.fail_next:
            raise self.fail_next.pop(0)
        if path == "api/v1/cache/upload-chunk":
            self.chunks.setdefault(body["handle"], {})[body["chunk_index"]] = ds.base64.b64decode(body["chunk_b64"])
            return {"received": len(self.chunks[body["handle"]]), "total": body["total_chunks"]}
        if path == "api/v1/cache/finalize":
            self.finalized.add(body["handle"])
            return {"size_bytes": sum(len(c) for c in self.chunks[body["handle"]].values())}
        assert body["cache_handle"] in self.finalized
        if path == "api/v1/tools/xlsx_check":
            return {"content": [{"type": "text", "text": "check"}], "_meta": {"verdict": "findings" if self.findings else "clean", "findings": self.findings}}
        if path == "api/v1/tools/xlsx_eval":
            assert len(body["formulas"]) <= 100
            return {"_meta": {"cell_values": [dict(index=i, **self.answer(f)) for i, f in enumerate(body["formulas"])]}}
        raise AssertionError(path)


def ds_date_serial(y, m, d):
    import datetime
    return float((datetime.date(y, m, d) - datetime.date(1899, 12, 30)).days)


class XlsxTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDatastash()
        self.engine = FakeEngine({("Summary", "B7"): {"type": "number", "value": 1234.5},
                                  ("Summary", "B8"): {"type": "string", "value": "Total \"net\""},
                                  ("Summary", "B9"): {"type": "number", "value": 46350.0},
                                  ("Bob's", "C1"): {"type": "number", "value": 3.0},
                                  ("Summary", "E1"): {"type": "error", "value": "#REF!"}})
        self.dir = harness.tmpdir(self, prefix="xlsx-test-", dir=TMP)
        for name, new in (("mcp_tool", self.fake.mcp_tool), ("api_json", self.fake.api_json), ("xt_engine", self.engine)):
            old = getattr(ds, name)
            setattr(ds, name, new)
            self.addCleanup(setattr, ds, name, old)
        old_sleep = ds.CiClient.__dict__["sleep"]
        ds.CiClient.sleep = staticmethod(lambda s: None)
        self.addCleanup(setattr, ds.CiClient, "sleep", old_sleep)
        fd, self.xlsx = tempfile.mkstemp(prefix="out-", suffix=".xlsx", dir=self.dir)
        with os.fdopen(fd, "wb") as f:
            f.write(b"PK\x03\x04" + CANARY * 50)

    def rows(self):
        return self.fake.tabs.get("Expected outputs", {})

    def expect(self, cell, kind, expected, tolerance=0, sheet="Summary"):
        self.fake._apply("Expected outputs", {"key": f"invoice / {sheet}!{cell}", "set": "invoice", "sheet": sheet, "cell": cell,
                                              "kind": kind, "expected": expected, "tolerance": tolerance})

    def test_record_then_pass_file_goes_only_to_the_engine(self):
        s = ds.xlsx_test(self.xlsx, "invoice", KEY, WB, ENGINE_KEY, record=True,
                         cells=ds.xt_parse_cells("Summary!B7, Summary!$B$8,'Bob''s'!c1,Summary!E1"), tolerance=0.01)
        self.assertEqual((s["recorded"], s["skipped"]), (3, ["invoice / Summary!E1"]))  # the error cell is not recorded
        r = self.rows()
        self.assertEqual((r["invoice / Summary!B7"]["kind"], r["invoice / Summary!B7"]["expected"], r["invoice / Summary!B7"]["tolerance"]), ("number", "1234.5", 0.01))
        self.assertEqual((r["invoice / Summary!B8"]["kind"], r["invoice / Summary!B8"]["expected"]), ("text", "Total \"net\""))
        self.assertEqual(r["invoice / Bob's!C1"]["expected"], "3")
        s = ds.xlsx_test(self.xlsx, "invoice", KEY, WB, ENGINE_KEY, sha=SHA, branch="main", run_url="https://ci.example/1")
        self.assertEqual((s["checked"], s["passed"], s["failed"], s["unknown"], s["stale"]), (3, 3, 0, 0, 0))
        b7 = self.rows()["invoice / Summary!B7"]
        self.assertEqual((b7["runs"], b7["fails"], b7["last_pass"], b7["last_actual"], b7["last_sha"]), (1, 0, True, "1234.5", SHA))
        run = self.fake.tabs["Output runs"][f"invoice / {SHA}"]
        self.assertEqual((run["checked"], run["passed"], run["failed"], run["unknown"], run["stale_cells"], run["branch"]), (3, 3, 0, 0, 0, "main"))
        # The comparison is the engine's formula; the quoted sheet name and the text's quotes are doubled.
        formulas = [f for p, b in self.engine.calls if p.endswith("xlsx_eval") for f in b["formulas"]]
        self.assertIn("=ABS('Summary'!B7-(1234.5))<=0.01", formulas)
        self.assertIn("=EXACT('Summary'!B8,\"Total \"\"net\"\"\")", formulas)
        self.assertIn("='Bob''s'!C1", formulas)
        # The file: one handle, finalized, every chunk to the engine; none of it to datastash.
        self.assertEqual(len(self.engine.finalized), 2)
        self.assertNotIn(CANARY.decode(), json.dumps(self.fake.calls))
        self.assertNotIn(ds.base64.b64encode(CANARY).decode()[:12], json.dumps(self.fake.calls))
        self.assertNotIn(ENGINE_KEY, json.dumps(self.fake.calls))

    def test_moved_not_known_and_stale_fail(self):
        self.expect("B7", "number", "1234.4", 0.05)  # moved by 0.1
        self.expect("B8", "text", "Total")  # moved
        self.expect("B9", "date", "2026-11-24")  # 46350 is 2026-11-24: passes
        self.expect("B8", "number", "5")  # text where a number is expected: the engine errors, not known
        self.fake._apply("Expected outputs", {"key": "invoice / Summary!ZZZZ1", "set": "invoice", "sheet": "Summary", "cell": "ZZZZ1", "kind": "number", "expected": "1"})
        self.fake._apply("Expected outputs", {"key": "other / Summary!B7", "set": "other", "sheet": "Summary", "cell": "B7", "kind": "number", "expected": "1"})
        self.engine.findings = [{"code": "stale_cached_value", "location": "Summary!B7"}, {"code": "error_cell", "location": "Summary!E1"}]
        s = ds.xlsx_test(self.xlsx, "invoice", KEY, WB, ENGINE_KEY, sha=SHA)
        out = {k: o for k, o, _a, _e in s["results"]}
        # B8 is keyed once (the number row replaced the text row of the same key).
        self.assertEqual(out, {"invoice / Summary!B7": "fail", "invoice / Summary!B8": "unknown", "invoice / Summary!B9": "pass",
                               "invoice / Summary!ZZZZ1": "unknown"})
        self.assertEqual((s["passed"], s["failed"], s["unknown"], s["stale"]), (1, 1, 2, 1))
        b7 = self.rows()["invoice / Summary!B7"]
        self.assertEqual((b7["last_pass"], b7["fails"], b7["last_actual"]), (False, 1, "1234.5"))
        self.assertEqual(self.rows()["invoice / Summary!B8"]["last_pass"], False)
        self.assertNotIn("runs", self.rows()["other / Summary!B7"])  # another set is never touched
        self.assertEqual(self.fake.tabs["Output runs"][f"invoice / {SHA}"]["stale_cells"], 1)

    def test_formulas_in_calls_of_at_most_100_and_chunked_upload(self):
        for n in range(1, 131):
            self.engine.cells[("Summary", f"A{n}")] = {"type": "number", "value": float(n)}
            self.expect(f"A{n}", "number", str(n))
        old = ds.XT_CHUNK
        ds.XT_CHUNK = 64
        self.addCleanup(setattr, ds, "XT_CHUNK", old)
        s = ds.xlsx_test(self.xlsx, "invoice", KEY, WB, ENGINE_KEY)
        self.assertEqual((s["checked"], s["passed"]), (130, 130))
        evals = [b for p, b in self.engine.calls if p.endswith("xlsx_eval")]
        self.assertEqual([len(b["formulas"]) for b in evals], [100, 100, 60])
        ups = [b for p, b in self.engine.calls if p.endswith("upload-chunk")]
        self.assertEqual(len({b["handle"] for b in ups}), 1)
        self.assertEqual([b["chunk_index"] for b in ups], list(range(len(ups))))
        self.assertGreater(len(ups), 1)
        h = ups[0]["handle"]
        with open(self.xlsx, "rb") as f:
            self.assertEqual(b"".join(self.engine.chunks[h][i] for i in range(len(ups))), f.read())
        self.assertNotIn("Output runs", self.fake.tabs)  # no --sha: no run row

    def test_rerecord_keeps_a_rows_tolerance_and_problems(self):
        self.expect("B7", "number", "1000", 0.5)
        ds.xlsx_test(self.xlsx, "invoice", KEY, WB, ENGINE_KEY, record=True)
        self.assertEqual((self.rows()["invoice / Summary!B7"]["expected"], self.rows()["invoice / Summary!B7"]["tolerance"]), ("1234.5", 0.5))
        with self.assertRaises(ds.CiProblem) as cm:
            ds.xlsx_test(self.xlsx, "nothing-here", KEY, WB, ENGINE_KEY)
        self.assertIn("--record", str(cm.exception))
        self.engine.fail_next = [ds.CiProblem("uploading the file to the engine: the engine refused the key (HTTP 401); check XFA_API_KEY")]
        with self.assertRaises(ds.CiProblem):
            ds.xlsx_test(self.xlsx, "invoice", KEY, WB, ENGINE_KEY)
        for bad in ("B7", "Summary!", "Summary!B0", "Summary!AAAA1"):
            with self.assertRaises(ds.CiProblem):
                ds.xt_parse_cells(bad)
        self.assertEqual(ds.xt_parse_cells("'a,b'!B2,x!c3"), [("a,b", "B2"), ("x", "C3")])
        self.assertIsNone(ds.xt_compare("'S'!A1", "number", "nan", 0))
        self.assertIsNone(ds.xt_compare("'S'!A1", "text", "x" * 5000, 0))
        self.assertIsNone(ds.xt_compare("'S'!A1", "number", "1", -1))

    def test_a_datastash_write_problem_keeps_the_engines_verdict(self):
        self.expect("B7", "number", "1")
        old = ds.CiClient.upsert

        def broken(self_, tab, rows, tag):
            raise ds.CiProblem("writing Expected outputs: datastash answered HTTP 500")
        ds.CiClient.upsert = broken
        self.addCleanup(setattr, ds.CiClient, "upsert", old)
        s = ds.xlsx_test(self.xlsx, "invoice", KEY, WB, ENGINE_KEY, sha=SHA)
        self.assertEqual(s["failed"], 1)
        self.assertIn("HTTP 500", s["write_problem"])

    def run_cli(self, *args, key=KEY, wb=WB, engine=ENGINE_KEY):
        env = dict(os.environ, DATASTASH_NO_NETWORK="1", DATASTASH_CI_RETRY_SECONDS="0", DATASTASH_AGENT_KEY=key,
                   DATASTASH_WORKBOOK_ID=wb, XFA_API_KEY=engine)
        return subprocess.run([sys.executable, "-I", BIN, "xlsx-test"] + list(args), capture_output=True, text=True,
                              env=env, timeout=60)

    def test_cli_exit_codes_and_one_line(self):
        good = ["--set", "invoice"]
        for args, key, wb, engine, want in (
                ([self.xlsx] + good, KEY, WB, ENGINE_KEY, "network off"),
                ([self.xlsx] + good, "", WB, ENGINE_KEY, "DATASTASH_AGENT_KEY"),
                ([self.xlsx] + good, KEY, WB, "", "XFA_API_KEY"),
                ([self.xlsx] + good, KEY, "nope", ENGINE_KEY, "DATASTASH_WORKBOOK_ID"),
                ([self.xlsx], KEY, WB, ENGINE_KEY, "--set is missing"),
                ([self.xlsx] + good + ["--sha", "zz"], KEY, WB, ENGINE_KEY, "--sha"),
                ([self.xlsx] + good + ["--cells", "S!A1"], KEY, WB, ENGINE_KEY, "--cells goes with --record"),
                ([self.xlsx] + good + ["--record", "--tolerance", "-1"], KEY, WB, ENGINE_KEY, "--tolerance"),
                ([self.xlsx] + good + ["--bogus"], KEY, WB, ENGINE_KEY, "unknown option"),
                (good, KEY, WB, ENGINE_KEY, "name one .xlsx file")):
            r = self.run_cli(*args, key=key, wb=wb, engine=engine)
            self.assertEqual(r.returncode, 2, (args, r.stdout, r.stderr))
            lines = r.stdout.strip().splitlines()
            self.assertEqual(len(lines), 1, r.stdout)
            self.assertIn("did not run", lines[0])
            self.assertIn(want, lines[0], args)
            self.assertNotIn(ENGINE_KEY, r.stdout + r.stderr)
            self.assertNotIn(KEY, r.stdout + r.stderr)
        h = self.run_cli("--help")
        self.assertEqual(h.returncode, 0)
        self.assertIn("usage: datastash xlsx-test", h.stdout)

    def test_cmd_prints_each_moved_cell_and_exits_1(self):
        self.expect("B7", "number", "1")
        self.expect("B9", "number", "46350")
        old_env = {k: os.environ.get(k) for k in ("DATASTASH_AGENT_KEY", "XFA_API_KEY", "DATASTASH_WORKBOOK_ID")}
        os.environ.update(DATASTASH_AGENT_KEY=KEY, XFA_API_KEY=ENGINE_KEY, DATASTASH_WORKBOOK_ID=WB)

        def restore():
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ds.cmd_xlsx_test([self.xlsx, "--set", "invoice", "--sha", SHA])
        self.assertEqual(code, 1)
        out = buf.getvalue()
        self.assertIn("invoice / Summary!B7 moved: expected 1, the engine reads 1234.5", out)
        self.assertIn("FAIL set invoice: 1 of 2 cells passed (1 moved, 0 not known, 0 stale)", out)
        self.expect("B7", "number", "1234.5")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ds.cmd_xlsx_test([self.xlsx, "--set", "invoice", "--sha", SHA])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertIn("ok set invoice: 2 of 2 cells passed", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
