#!/usr/bin/env python3
"""Offline tests for XLI-392 N2 (plugin 1.0.5): the step heartbeat.
- `datastash heartbeat STEP [--tab T] [--column C]` names this folder's step; `off` stops it; DATASTASH_STEP names it too.
- While this folder's agent uses tools (PostToolUse), the hook starts a background touch at most once a minute; never in
  another hook event, a folder with no step, or a session with no agent of its own.
- The background run calls only POST /api/v1/agent/rows/touch (x-live #484) with the workbook, tab, key and column: it
  never adds a row; a 404 (no such row) is logged once, and nothing else is sent.
No network, no key; under test/harness.py.
Run: python3 -I test/test_step_heartbeat.py"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import ds, TMP  # noqa: E402  (the same harness and plugin copy)
import harness  # noqa: E402

WB = "0b6a3f9e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
KEY = "ldgr_ag_testkeytestkeytestkey"
SID = "5b6b3ff3-d567-4579-81fc-a8eedc898995"


class Heartbeat(unittest.TestCase):
    def setUp(self):
        self.folder = os.path.realpath(harness.tmpdir(self, prefix="proj-", dir=TMP))
        ds.ensure_dirs()
        ds.write_private(ds.conf_path("web-app"), json.dumps({"name": "web-app", "workbook_id": WB,
                                                              "agent_id": "a1b2c3d4-0000-4000-8000-000000000001",
                                                              "projects": [self.folder]}))
        self.saved = {}
        self.spawned, self.posts = [], []
        self.answer = None

        def api(method, path, key, body=None, timeout=40):
            self.posts.append((method, path, key, json.loads(json.dumps(body))))
            if self.answer is not None:
                raise self.answer
            return {"row_id": 1, "row_version": 2, "column": body.get("column"), "touched_at": "2026-11-24T10:00:00.000Z"}
        for name, new in (("spawn_detached", lambda argv, cwd: self.spawned.append((argv, cwd))),
                          ("read_key", lambda s: KEY), ("api_json", api)):
            self.saved[name] = getattr(ds, name)
            setattr(ds, name, new)
        self.env = {k: os.environ.get(k) for k in ("CLAUDE_PROJECT_DIR", "DATASTASH_STEP", "DATASTASH_STEP_TAB")}
        os.environ["CLAUDE_PROJECT_DIR"] = self.folder
        os.environ.pop("DATASTASH_STEP", None)
        os.environ.pop("DATASTASH_STEP_TAB", None)
        for p in [ds.STEP_CONF] + [os.path.join(ds.STATE, n) for n in os.listdir(ds.STATE) if n.startswith("step-touch-")]:
            if os.path.exists(p):
                os.unlink(p)
        self.cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd)
        for k, v in self.saved.items():
            setattr(ds, k, v)
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def say(self, *args):
        with redirect_stdout(io.StringIO()) as out:
            rc = ds.cmd_heartbeat(list(args))
        return rc, out.getvalue()

    def tool(self, event="PostToolUse"):
        ds.step_touch_hook({"session_id": SID, "hook_event_name": event, "cwd": self.folder, "agent_id": "x",
                            "tool_name": "Bash", "tool_input": {"command": "ls"}})

    def age(self):
        for n in os.listdir(ds.STATE):
            if n.startswith("step-touch-"):
                os.utime(os.path.join(ds.STATE, n), (1, 1))

    def test_off_until_named_then_at_most_once_a_minute(self):
        self.tool()
        self.assertEqual(self.spawned, [])
        rc, out = self.say()
        self.assertIn("heartbeat is off", out)
        rc, out = self.say("step 3", "--tab", "Steps")
        self.assertEqual(rc, 0, out)
        self.assertIn("never adds a row", out)
        self.assertEqual(ds.step_for(self.folder), {"key": "step 3", "tab": "Steps", "column": "last_seen"})
        for event in ("PreToolUse", "Stop", "SessionStart"):
            self.tool(event)
        self.assertEqual(self.spawned, [])
        self.tool()
        self.tool()
        self.assertEqual(len(self.spawned), 1)
        argv, cwd = self.spawned[0]
        self.assertEqual(argv[-4:-1], ["heartbeat", "run", "web-app"])
        self.assertEqual(cwd, self.folder)
        self.age()  # a minute later
        self.tool()
        self.assertEqual(len(self.spawned), 2)
        rc, out = self.say("off")
        self.age()
        self.tool()
        self.assertEqual(len(self.spawned), 2)

    def test_the_environment_names_the_step(self):
        os.environ["DATASTASH_STEP"] = "XLI-12 build"
        self.assertEqual(ds.step_for(self.folder), {"key": "XLI-12 build", "tab": "Card steps", "column": "last_seen"})
        os.environ["DATASTASH_STEP_TAB"] = "Steps"
        self.assertEqual(ds.step_for(self.folder)["tab"], "Steps")
        self.tool()
        self.assertEqual(len(self.spawned), 1)

    def test_no_agent_of_its_own_no_touch(self):
        self.say("s1")
        old = ds._own_agent
        ds._own_agent = lambda inp: None
        self.addCleanup(setattr, ds, "_own_agent", old)
        self.tool()
        self.assertEqual(self.spawned, [])

    def run_bg(self):
        self.tool()
        argv, cwd = self.spawned[-1]
        os.chdir(cwd)
        return ds.cmd_heartbeat(argv[-3:])

    def test_the_run_only_touches_and_never_adds_a_row(self):
        self.say("s1", "--column", "beat")
        self.assertEqual(self.run_bg(), 0)
        self.assertEqual(self.posts, [("POST", "/api/v1/agent/rows/touch", KEY,
                                       {"workbook_id": WB, "tab": "Card steps", "key": "s1", "column": "beat"})])
        # no such row: 404, logged once, nothing else sent (no add)
        self.answer = ds.ApiError(404, {"code": "not_found"})
        self.age()
        self.run_bg()
        self.age()
        self.run_bg()
        self.assertEqual({p[1] for p in self.posts}, {"/api/v1/agent/rows/touch"})
        self.assertEqual(len(self.posts), 3)
        with open(ds.log_path("web-app")) as f:
            lines = [x for x in f.read().splitlines() if "heartbeat:" in x]
        self.assertEqual(len([x for x in lines if "no row" in x]), 1)
        self.assertIn("nothing added", lines[-1])
        self.assertNotIn(KEY, "\n".join(lines))

    def test_bad_input_is_refused(self):
        self.assertEqual(self.say("s1", "--tab")[0], 1)
        self.assertEqual(self.say("--x")[0], 1)
        self.assertEqual(self.say("s\x01")[0], 1)
        self.assertEqual(ds.cmd_heartbeat(["run", "../x", "/tmp/x"]), 1)
        self.assertEqual(ds.cmd_heartbeat(["run", "web-app", "/tmp/step-touch-0123456789abcdef.json"]), 1)
        self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main()
