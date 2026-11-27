#!/usr/bin/env python3
"""Offline tests for XLI-425 (plugin 1.0.22): `datastash share TAB --with AGENT[,AGENT] [--write] [--workbook ID]`.
- It is the connector's ledger_agent_workbook share over REST: one POST /api/v1/agent/tabs/share with the agent's own
  key and its own workbook, {workbook_id, tab, agents, access}; access "read" unless --write.
- --with takes a comma list, may repeat (and --with=A,B); names are trimmed and sent once each.
- Bad arguments die with the usage line and send nothing; a folder with no agent sends nothing.
- datastash's refusal (403 not_tab_maker: only the tab's maker or the person shares it) is printed as untrusted data and
  exits 1, as `datastash api` does.
No network, no key; under test/harness.py.
Run: python3 -I test/test_share.py"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import ds, TMP  # noqa: E402  (the same harness and plugin copy)
import harness  # noqa: E402

WB = "0b6a3f9e-1c2d-4e5f-8a9b-0c1d2e3f4a5c"
OTHER_WB = "0b6a3f9e-1c2d-4e5f-8a9b-0c1d2e3f4a5d"
KEY = "ldgr_ag_sharetestkeysharetestkey"
SLUG = "share-agent"


class ShareBase(unittest.TestCase):
    def setUp(self):
        self.folder = os.path.realpath(harness.tmpdir(self, prefix="proj-", dir=TMP))
        ds.ensure_dirs()
        ds.write_private(ds.conf_path(SLUG), json.dumps({"name": SLUG, "workbook_id": WB, "projects": [self.folder]}))
        self.calls = []
        self.refuse = None

        def fake_http(method, path, key=None, body=None, form=None, timeout=40, **kw):
            self.calls.append((method, path, key, json.loads(json.dumps(body))))
            if self.refuse is not None:
                raise ds.ApiError(*self.refuse)
            return 200, "application/json", json.dumps({"tab": body["tab"], "shared": body["agents"],
                                                         "shared_with": [{"agent": n, "access": body["access"]}
                                                                         for n in body["agents"]]})
        self.saved = {}
        for name, new in (("http", fake_http), ("read_key", lambda s: KEY), ("auto_setup_check", lambda *a, **k: None),
                          ("spawn_detached", lambda argv, cwd: None)):
            self.saved[name] = getattr(ds, name)
            setattr(ds, name, new)
        self.env = {k: os.environ.get(k) for k in ("CLAUDE_PROJECT_DIR",)}
        os.environ["CLAUDE_PROJECT_DIR"] = self.folder
        self.addCleanup(self.clear_outbox)

    def clear_outbox(self):
        d = ds.outbox_dir(SLUG)
        for n in (os.listdir(d) if os.path.isdir(d) else []):
            os.unlink(os.path.join(d, n))  # test code clearing its own throwaway folder

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(ds, k, v)
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def share(self, *args):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            try:
                code = ds.cmd_share(list(args))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def sent(self):
        posts = [c for c in self.calls if c[0] == "POST" and c[1] == ds.AGENT_TAB_SHARE_PATH]
        self.assertEqual(len(posts), 1, self.calls)
        self.assertEqual(posts[0][2], KEY)
        return posts[0][3]


class Sends(ShareBase):
    def test_read_by_default_on_the_agents_own_workbook(self):
        code, out, _ = self.share("Setup", "--with", "Builder")
        self.assertEqual(code, 0)
        self.assertEqual(self.sent(), {"workbook_id": WB, "tab": "Setup", "agents": ["Builder"], "access": "read"})
        self.assertIn("<<datastash-data", out)
        self.assertIn('"access": "read"', out)

    def test_write_and_a_comma_list(self):
        code, _, _ = self.share("Asks", "--with", "Builder, Reviewer,,Builder", "--write")
        self.assertEqual(code, 0)
        self.assertEqual(self.sent(), {"workbook_id": WB, "tab": "Asks", "agents": ["Builder", "Reviewer"],
                                       "access": "write"})

    def test_repeated_with_equals_form_and_workbook(self):
        code, _, _ = self.share("--write", "--with=Builder", "Card runs", "--with", "Reviewer", "--workbook", OTHER_WB)
        self.assertEqual(code, 0)
        self.assertEqual(self.sent(), {"workbook_id": OTHER_WB, "tab": "Card runs", "agents": ["Builder", "Reviewer"],
                                       "access": "write"})

    def test_the_command_table_has_it(self):
        saved = sys.argv
        sys.argv = ["datastash", "share", "Setup", "--with", "Builder"]
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(ds.main(), 0)
        finally:
            sys.argv = saved
        self.assertEqual(self.sent()["agents"], ["Builder"])


class Refuses(ShareBase):
    def test_bad_arguments_send_nothing(self):
        for args in ((), ("Setup",), ("Setup", "--with"), ("Setup", "--with", ""), ("Setup", "--with", " , "),
                     ("Setup", "Other", "--with", "Builder"), ("Setup", "--with", "Builder", "--read-write"),
                     ("--with", "Builder"), ("Setup", "--with", "--write")):
            code, _, err = self.share(*args)
            self.assertNotEqual(code, 0, args)
            self.assertIn(ds.SHARE_USAGE, err, args)
        self.assertEqual(self.calls, [])

    def test_not_the_tabs_maker_is_datastashs_refusal_printed_as_data(self):
        self.refuse = (403, {"code": "not_tab_maker", "message": "only the agent that made tab \"Setup\", or the person, can share it."})
        code, out, _ = self.share("Setup", "--with", "Builder", "--write")
        self.assertEqual(code, 1)
        self.assertEqual(self.sent()["access"], "write")
        self.assertIn("not_tab_maker", out)
        self.assertIn("HTTP 403", out)
        self.assertEqual(ds.outbox_entries(SLUG), [])  # a refusal is never kept for later

    def test_no_agent_here_sends_nothing(self):
        os.environ["CLAUDE_PROJECT_DIR"] = os.path.realpath(harness.tmpdir(self, prefix="none-", dir=TMP))
        code, _, err = self.share("Setup", "--with", "Builder")
        self.assertNotEqual(code, 0)
        self.assertIn("no agent workbook is linked", err)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
