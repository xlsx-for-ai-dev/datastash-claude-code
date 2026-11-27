"""Offline tests for plugin 1.0.21 (card 8e, XLI-424): setup finishes itself, the Setup tab row, "who are you", and the
full-DDS checklist at install and update.
- Signed in (a datastash connector answer seen here): a folder with no agent of its own gets the plugin's own two
  connector calls as one wake; the key is collected as at install; never twice, never after the person uninstalled that
  folder, never while their typed install runs, never in the home folder.
- Not signed in: said plainly (session start and one wake a day), nothing tried, no /mcp text anywhere.
- The Setup row: the hand-made tab's columns, keyed by agent; made when missing; identity kept where the row has it;
  written again (last checked moves).
- "who are you" (self, all, named) is answered by the plugin itself (decision block), from the tab or its copy.
- The checklist: every item, a self-check with the one step and who, what's new; once per plugin version.
No network; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_setup.py"""
import io
import json
import os
import sys
import time
import unittest
import urllib.parse
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_install import ANSWER, KEY, MINT, OWN, WB, Base, ds  # noqa: E402
import harness  # noqa: E402

TAB_ID = "5d7f2e88-82d2-42e5-a2f4-35645c9c73e8"


class SetupBase(Base):
    def setUp(self):
        super().setUp()
        self.keep8e = {k: getattr(ds, k) for k in ("mcp_tool", "AUTO_SETUP_WAIT", "_last_names_path", "_tmux_name",
                                                   "marketplace_autoupdate", "_installed_version")}
        ds.AUTO_SETUP_WAIT = 0
        names = os.path.join(ds.CONF + "-names", "last.json")
        ds._last_names_path = lambda: names
        ds._tmux_name = lambda: None
        ds.marketplace_autoupdate = lambda: True
        ds._installed_version = lambda: "1.0.21"
        os.makedirs(ds.STATE, exist_ok=True)
        self.rows = {}  # the Setup tab on the fake server: None = no such tab
        self.tab = True
        self.mcp = []

        def fake_http(method, path, key=None, body=None, form=None, timeout=40, accept=None):
            self.calls.append((method, path, key, body))
            u = urllib.parse.urlparse(path)
            q = dict(urllib.parse.parse_qsl(u.query))
            if u.path == ds.ROWS_PATH and method == "GET" and q.get("tab") == ds.SETUP_TAB:
                if not self.tab:
                    raise ds.ApiError(404, {"error": "not_found"})
                rows = [{"row_id": i, "cells": c} for i, c in enumerate(self.rows.values())]
                return 200, "application/json", json.dumps({"rows": rows, "more": False})
            if u.path == "/api/v1/agent/tabs" and method == "POST":
                self.tab = True
                return 200, "application/json", json.dumps({"tab_id": TAB_ID, "name": body["name"]})
            got = self.answers.get(path, (404, {}))
            if got[0] >= 400:
                raise ds.ApiError(got[0], got[1])
            return got[0], "application/json", json.dumps(got[1])
        ds.http = fake_http

        def fake_mcp(key, name, args, timeout=60):
            self.mcp.append((name, args))
            if name == "ledger_rows_upsert":
                for r in args["rows"]:
                    self.rows[r["agent"]] = dict(r)
                return False, {"row_ids": [1]}
            return False, {}
        ds.mcp_tool = fake_mcp

    def tearDown(self):
        for k, v in self.keep8e.items():
            setattr(ds, k, v)
        super().tearDown()

    def signed_in(self):
        ds.write_private(ds._connector_ok_path(), json.dumps({"at": int(time.time())}))

    def inp(self, sid="s1", folder=None):
        return {"session_id": sid, "cwd": folder or self.folder, "hook_event_name": "SessionStart",
                "transcript_path": self.transcript(sid)}

    def wake(self, sid="s1", folder=None):
        i = self.inp(sid, folder)
        return ds.auto_setup_wake(i, folder or self.folder, ds.this_session(i))

    def collect(self):
        """The model's two calls from the wake, and the plugin's collect."""
        self.tool("PostToolUse", "ledger_agent_register", {"name": "web-app"},
                  {"agent_id": OWN, "notes_tabs": [{"workbook_id": WB}]})
        bound = self.tool("PreToolUse", "ledger_agent_key", {"name": "web-app", "only_if_no_key": True})
        self.assertIn("code_challenge", bound["hookSpecificOutput"]["updatedInput"])
        self.answers[ds.COLLECT_PATH] = (200, {"agent_id": OWN, "name": "web-app", "workbook_id": WB, "key": KEY,
                                               "answer_key": ANSWER})
        return self.tool("PostToolUse", "ledger_agent_key", {"name": "web-app"},
                         {"collect": "ready", "mint_id": MINT, "name": "web-app"})


class SelfSetup(SetupBase):
    def test_a_signed_in_computer_sets_the_folder_up_with_no_person_step(self):
        self.signed_in()
        ai = self.wake()
        self.assertEqual(ai, ds.AUTO_SETUP_AI.format(name="web-app"))
        self.assertIn('ledger_agent_register with name "web-app"', ai)
        self.assertNotIn("/mcp", ai)
        out = self.collect()
        self.assertEqual(out["systemMessage"], ds.MINT_LINE.format(name="web-app"))
        self.assertEqual(ds.read_key("web-app"), KEY)
        self.assertEqual(ds.agent_for(self.folder)["name"], "web-app")
        self.assertIn("full-DDS checklist", out["hookSpecificOutput"]["additionalContext"])  # at install
        self.assertTrue(any("setup-row" in argv for argv, _cwd in self.spawned))  # and its Setup row
        self.assertNotIn(KEY, json.dumps(out))
        self.assertIsNone(self.wake())  # set up: never again

    def test_the_listener_wakes_the_session_once_with_the_steps(self):
        self.signed_in()
        err = io.StringIO()
        ds.hook_input = lambda: self.inp()
        with redirect_stderr(err):
            self.assertEqual(ds._listen_start(self.inp(), None, None), 2)
        self.assertIn('ledger_agent_register with name "web-app"', err.getvalue())
        self.assertIsNone(self.wake())  # one try per folder in AUTO_SETUP_RETRY

    def test_a_connector_answer_marks_the_computer_signed_in_and_a_refusal_unmarks_it(self):
        self.assertIsNone(ds.signin_known())
        self.tool("PostToolUse", "ledger_agents_list", {}, {"agents": []})
        self.assertEqual(ds.signin_known(), "in")
        inp = {"session_id": "s1", "hook_event_name": "PostToolUseFailure", "tool_name": "mcp__x_datastash__checkin",
               "tool_input": {}, "cwd": self.folder, "error": "HTTP 401 Unauthorized"}
        self.hook(ds.cmd_agent_tool, inp)
        self.assertEqual(ds.signin_known(), "out")

    def test_never_after_the_person_uninstalled_that_folder(self):
        self.signed_in()
        os.makedirs(os.path.dirname(ds._last_names_path()), exist_ok=True)
        with open(ds._last_names_path(), "w") as f:
            json.dump({"names": [{"name": "web-app", "folders": [os.path.realpath(self.folder)], "sids": []}]}, f)
        self.assertIsNone(self.wake())

    def test_never_while_the_typed_install_runs_nor_in_the_home_folder_nor_a_folder_left_off(self):
        self.signed_in()
        ds.save_cmd("s1", {"kind": "install", "name": "web-app", "at": time.time()})
        self.assertIsNone(self.wake())
        self.assertIsNone(self.wake(sid="s2", folder=ds.HOME))
        ds.record_left_off([os.path.realpath(self.folder)])
        self.assertIsNone(self.wake(sid="s3"))

    def test_session_start_line_when_signed_in(self):
        self.signed_in()
        i = self.inp()
        self.assertEqual(ds.no_agent_setup_note(i, self.folder, ds.this_session(i)), ds.SETTING_UP_AI)


class NotSignedIn(SetupBase):
    def test_nothing_is_tried_and_the_check_is_kept_for_the_session_start(self):
        self.assertIsNone(self.wake())  # no wake: the session start says it
        self.assertFalse([c for c in self.calls])  # nothing sent anywhere
        self.assertIsNone(ds.agent_for(self.folder))
        self.assertEqual(ds.signin_known(), "out")
        i = self.inp(sid="s2")
        self.assertEqual(ds.no_agent_setup_note(i, self.folder, ds.this_session(i)), ds.NOT_SIGNED_IN_AI)
        self.assertIn(ds.NOT_SIGNED_IN_LINE, ds.NOT_SIGNED_IN_AI)

    def test_agents_with_their_own_keys_here_get_no_not_signed_in_line(self):
        ds._note_signin("out", "mcp list")
        ds.save_agent("other", OWN, KEY, WB, os.path.join(os.path.dirname(self.folder), "other"))
        i = self.inp()
        self.assertIsNone(ds.no_agent_setup_note(i, self.folder, ds.this_session(i)))

    def test_session_start_says_it_and_has_no_mcp_text(self):
        ds._note_signin("out", "mcp list")
        i = self.inp()
        note = ds.no_agent_setup_note(i, self.folder, ds.this_session(i))
        self.assertEqual(note, ds.NOT_SIGNED_IN_AI)
        for t in (ds.NOT_SIGNED_IN_AI, ds.NOT_SIGNED_IN_LINE, ds.SETTING_UP_AI, ds.CHECKING_AI, ds.AUTO_SETUP_AI,
                  ds.checklist_text(None, self.folder)):
            self.assertNotIn("/mcp", t)
            self.assertNotIn("Authenticate", t)

    def test_the_row_says_not_signed_in(self):
        ds._note_signin("out", "mcp list")
        agent = {"name": "web-app", "slug": "web-app", "workbook_id": WB}
        row = ds.setup_row_cells(agent, self.folder)
        self.assertEqual(row["registered"], "no: this computer is not signed in")
        self.assertIn("computer not signed in", row["typed sync"])


class SetupRow(SetupBase):
    def agent(self):
        self.installed()
        return ds.agent_for(self.folder)

    def test_the_row_has_the_hand_made_columns_and_is_made_with_its_tab(self):
        a = self.agent()
        self.tab = False
        self.assertEqual(ds.write_setup_row(a, self.folder), "written")
        made = [c for c in self.calls if c[0] == "POST" and c[1] == "/api/v1/agent/tabs"][0][3]
        self.assertEqual([c["name"] for c in made["columns"]], list(ds.SETUP_COLUMNS))
        self.assertEqual(made["key_column"], "agent")
        self.assertEqual(ds.SETUP_COLUMNS, ("agent", "other names", "line", "does", "computer / OS user", "reach",
                                            "plugin (installed / running)", "registered", "typed sync",
                                            "full-DDS missing", "next step", "who does it", "last checked"))
        row = self.rows["web-app"]
        self.assertEqual(list(row), list(ds.SETUP_COLUMNS))
        self.assertEqual(row["registered"], "yes")
        self.assertEqual(row["plugin (installed / running)"], f"1.0.21 / {ds.plugin_version()}")
        self.assertIn("A3", row["full-DDS missing"])  # no typed sync yet
        self.assertEqual(row["who does it"], "the person")
        self.assertEqual(row["next step"], 'type "datastash sync" in this session')
        up = [a for n, a in self.mcp if n == "ledger_rows_upsert"][0]
        self.assertEqual((up["tab"], up["key"], up["workbook_id"]), ("Setup", "agent", WB))

    def test_the_persons_own_words_stay_and_the_rows_key_spelling_is_kept(self):
        a = self.agent()
        self.rows["WEB-APP"] = {"agent": "WEB-APP", "other names": "webby (agent-mail)", "line": "the web line",
                                "does": "builds the site", "computer / OS user": "build box / web",
                                "reach": "agent-mail webby", "last checked": "old"}
        ds.write_setup_row(a, self.folder)
        row = self.rows["WEB-APP"]
        self.assertEqual((row["line"], row["does"], row["reach"], row["other names"]),
                         ("the web line", "builds the site", "agent-mail webby", "webby (agent-mail)"))
        self.assertNotEqual(row["last checked"], "old")
        self.assertNotIn("web-app", self.rows)  # updated, not a second row

    def test_session_start_kicks_the_row_in_the_background(self):
        self.agent()
        ds.hook_input = lambda: self.inp()
        ds.CHECK_WAIT_SECONDS_SAVED = ds.CHECK_WAIT_SECONDS
        ds.setup_row_kick(ds.agent_for(self.folder), self.folder)
        argv = [a for a, _c in self.spawned if "setup-row" in a][0]
        self.assertEqual(argv[2:6], ["--agent", "web-app", "setup-row", "--retry"])


class WhoAreYou(SetupBase):
    def setUp(self):
        super().setUp()
        self.installed()
        self.rows["web-app"] = {"agent": "web-app", "other names": "webby (agent-mail)", "line": "web",
                                "does": "builds the site", "computer / OS user": "box / web", "reach": "datastash inbox"}
        self.rows["EPM"] = {"agent": "EPM", "other names": "epm (agent-mail)", "line": "engine", "does": "engine PM",
                            "computer / OS user": "box / epm", "reach": "/home/epm/inbox"}

    def ask(self, text):
        out, _pid = self.prompt(text)
        return out

    def test_self(self):
        out = self.ask("who are you?")
        self.assertEqual(out["decision"], "block")  # shown by the plugin itself: no model turn
        self.assertIn("Who I am", out["reason"])
        self.assertIn("builds the site", out["reason"])
        self.assertIn("Line: web", out["reason"])

    def test_all(self):
        out = self.ask("Who are you all")
        self.assertIn("EPM (epm (agent-mail)): engine PM", out["reason"])
        self.assertIn("web-app", out["reason"])
        self.assertFalse([c for c in self.calls if c[1].startswith("/api/v1/agent/inbox")])  # no agent woken

    def test_named_by_name_or_other_name(self):
        self.assertIn("engine PM", self.ask("who is epm")["reason"])
        self.assertIn("engine PM", self.ask("who are you EPM")["reason"])
        self.assertIn("builds the site", self.ask("who is webby")["reason"])

    def test_an_unknown_name_is_an_ordinary_prompt(self):
        out = self.ask("who is online")
        self.assertFalse((out or {}).get("decision"))

    def test_the_computers_copy_answers_when_datastash_is_down(self):
        self.ask("who are you all")  # fills the copy

        def down(*a, **k):
            raise ds.ApiError(503, {})
        ds.http = down
        self.assertIn("engine PM", self.ask("who is epm")["reason"])


class Checklist(SetupBase):
    def test_every_item_the_self_check_and_whats_new(self):
        self.installed()
        a = ds.agent_for(self.folder)
        text = ds.checklist_text(a, self.folder, version="1.0.21")
        for iid in ("A1", "A2", "A3", "A4", "A5", "B6", "B7", "B8", "B9", "B11", "C12", "C21", "D1", "D2"):
            self.assertIn(f"{iid}: ", text)
        self.assertIn("Self-check, missing:", text)
        self.assertIn("A3: The computer is unlocked", text)
        self.assertIn("Step: type **datastash sync** in this session (the person).", text)
        self.assertIn("For the person to type: A3: type **datastash sync** in this session", text)
        self.assertIn("What's new for your agent in 1.0.21:", text)
        self.assertNotIn("A2: This folder", text.split("Self-check")[1])  # it has its key: not missing

    def test_given_once_per_plugin_version(self):
        self.installed()
        a = ds.agent_for(self.folder)
        self.assertIsNotNone(ds.checklist_note(a, self.folder))
        self.assertIsNone(ds.checklist_note(a, self.folder))
        ds.write_private(ds._checklist_mark("web-app"), json.dumps({"version": "1.0.19"}))  # an update
        self.assertIsNotNone(ds.checklist_note(a, self.folder))

    def test_sync_on_is_not_missing(self):
        self.installed()
        keep = ds.sync_on
        ds.sync_on = lambda: True
        try:
            text = ds.checklist_text(ds.agent_for(self.folder), self.folder)
        finally:
            ds.sync_on = keep
        self.assertNotIn("A3:", text.split("Self-check")[1])

    def test_self_check_command(self):
        self.installed()
        out = io.StringIO()
        from contextlib import redirect_stdout
        with redirect_stdout(out):
            self.assertEqual(ds.cmd_self_check(["--folder", self.folder]), 0)
        self.assertIn("Full datastash (DDS) checklist", out.getvalue())
        self.assertNotIn("**", out.getvalue())


if __name__ == "__main__":
    unittest.main()
