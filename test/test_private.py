#!/usr/bin/env python3
"""Offline tests for XLI-328 card 3 (plugin 0.9.7): private folders, the plugin side. Card 2 (server) answers the
approval poll, register and hello with private: true|false and refuses a private agent's sharing calls.
- The plugin never sets private. "keep X private" (typed) shows the link line once datastash's answers carry the
  private field, else "Private mode is coming"; it makes no call and records nothing.
- A folder is recorded private only from the plugin's own call to datastash that answers private: true (approval poll,
  register or hello), and cleared only on private: false. A missing field changes nothing.
- In a private folder: `token --header` hands the connector the agent's own key (never the person's login); when the
  helper can't tell the folder, the PreToolUse hook denies the datastash connector there. Status-set and status-send,
  hello's causes, the heartbeat, the map's tab share (local map kept) and the lookup feed setup stay on this computer;
  the map, `where` and lookup counting keep working. The person's list shows "X: private".
- auto-name and link refuse to put a private folder's agent into another folder, or another agent into it.
No network, no key; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_private.py"""
import io
import json
import os
import re
import sys
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness  # noqa: E402,F401
from test_off import OffBase, AGENT_ID, mcp_answer  # noqa: E402
from test_status import ds, WB, BIN, TMP  # noqa: E402
from test_projectmap import MapBase  # noqa: E402

OWN_ID = "11111111-aaaa-bbbb-cccc-000000000001"
OTHER_ID = "11111111-aaaa-bbbb-cccc-000000000002"
COMING = "Private mode is coming."
LINK_LINE = f"To keep web-app private, press 'Turn private on' next to it on {ds.AGENTS_URL}. Until you do, it is on datastash."
FEED_LINE = "Not set up: a datastash feed would share it with your other agents."


def deny_of(out):
    return ((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision")


class PrivateBase(OffBase):
    def setUp(self):
        super().setUp()
        ds.write_private(ds.conf_path("web-app"), json.dumps({"name": "web-app", "agent_id": OWN_ID, "workbook_id": WB,
                                                              "projects": [self.folder]}))
        if os.path.exists(ds.PRIVATE_PATH):
            os.replace(ds.PRIVATE_PATH, os.path.join(TMP, f"old-private-{time.time()}"))
        self.env.setdefault("CLAUDE_CODE_MCP_SERVER_URL", os.environ.get("CLAUDE_CODE_MCP_SERVER_URL"))

    def make_private(self, agent_id=OWN_ID):
        self.assertTrue(ds.note_private({"agent_id": agent_id, "private": True}, agent_id))

    def agent(self):
        return ds.agent_for(self.folder)

    def token(self, folder="same"):
        os.environ["CLAUDE_CODE_MCP_SERVER_URL"] = ds.CONNECTOR_URL
        if folder != "same":
            if folder is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = folder
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            self.assertEqual(ds.cmd_token(["--header"]), 0)
        return json.loads(buf.getvalue())


class NeverSetsPrivate(PrivateBase):
    def test_keep_private_without_the_field_is_still_coming(self):
        text = self.prompt("keep web-app private")
        self.assertTrue(text.startswith(COMING), text)
        self.assertEqual(self.calls, [])
        self.assertFalse(ds.is_private(self.folder))

    def test_keep_private_with_the_field_gives_the_link_line_and_sets_nothing(self):
        ds.note_private({"agent_id": OWN_ID, "private": False}, OWN_ID)
        self.assertTrue(ds.private_field_seen())
        text = self.prompt("keep web-app private")
        self.assertEqual(text, LINK_LINE)
        self.assertEqual(self.calls, [])  # no call to datastash: the person presses Private on the agents page
        self.assertFalse(ds.is_private(self.folder))

    def test_keep_private_for_a_private_folder_says_so(self):
        self.make_private()
        text = self.prompt("keep web-app private")
        self.assertIn('"web-app" is private', text)
        self.assertNotIn("Until you do", text)

    def test_no_request_the_plugin_sends_carries_private(self):
        with open(BIN) as f:
            src = f.read()
        # private is only ever read from an answer; no body the plugin builds names it
        self.assertEqual(re.findall(r"[\"']private[\"']\s*:(?!\s*$)", src, re.M), [])
        # its def, approval, register, hello, and X6's typed "make <name> private|public" answer (plugin 1.0.1)
        self.assertEqual(len(re.findall(r"\bnote_private\(", src)), 5)


class Recording(PrivateBase):
    def waiting(self):
        other = self.other_folder("docs")
        ds._set_enrolled(other, {"name": "docs", "agent_id": OTHER_ID, "proof": "p" * 43, "login": "login1", "at": 1})
        return other

    def approval_answers(self, *answers):
        seq = list(answers)

        def fake(method, path, key=None, body=None, form=None, timeout=40, **kw):
            self.calls.append((method, path, key, body))
            if path == ds.AGENT_REGISTER_PATH and (body or {}).get("agent_id"):  # the approval route is gone (XLI-390 X4)
                return 200, "application/json", json.dumps(seq.pop(0) if len(seq) > 1 else seq[0])
            raise ds.ApiError(404, {})
        ds.http = fake

    def test_the_approval_poll_records_private_only_from_private_true(self):
        other = self.waiting()
        self.approval_answers({"agent_id": OTHER_ID, "approval": "waiting_for_person"})
        self.assertEqual(ds.advance(other, "dsat_person"), "waiting")
        self.assertFalse(ds.is_private(other))  # no field: nothing changes
        self.assertFalse(ds.private_field_seen())
        self.approval_answers({"agent_id": OTHER_ID, "approval": "waiting_for_person", "private": True})
        ds.advance(other, "dsat_person")
        self.assertTrue(ds.is_private(other))
        self.approval_answers({"agent_id": OTHER_ID, "approval": "waiting_for_person"})
        ds.advance(other, "dsat_person")
        self.assertTrue(ds.is_private(other))  # a missing field never clears it
        self.approval_answers({"agent_id": OTHER_ID, "approval": "waiting_for_person", "private": "yes"})
        ds.advance(other, "dsat_person")
        self.assertTrue(ds.is_private(other))  # only a real false clears it
        self.approval_answers({"agent_id": OTHER_ID, "approval": "waiting_for_person", "private": False})
        ds.advance(other, "dsat_person")
        self.assertFalse(ds.is_private(other))

    def test_an_answer_about_another_agent_records_nothing(self):
        other = self.waiting()
        self.approval_answers({"agent_id": OWN_ID, "approval": "waiting_for_person", "private": True})
        ds.advance(other, "dsat_person")
        self.assertFalse(ds.is_private(other))
        self.assertFalse(ds.is_private(self.folder))

    def test_a_404_noagent_answer_changes_nothing(self):
        self.make_private()
        ds.note_private({"code": "noagent"}, OWN_ID)
        ds.note_private(None, OWN_ID)
        ds.note_private({"private": False}, None)  # no agent asked about: nothing
        self.assertTrue(ds.is_private(self.folder))

    def test_register_records_private(self):
        other = self.other_folder("api")
        ds.recent_folders = lambda *a, **k: [other]

        def fake(method, path, key=None, body=None, form=None, timeout=40, **kw):
            self.calls.append((method, path, key, body))
            if path == ds.AGENT_REGISTER_PATH:
                return 200, "application/json", json.dumps({"agent_id": OTHER_ID, "name": body["name"], "private": True})
            if path.startswith(ds.AGENT_APPROVAL_PATH):
                return 200, "application/json", json.dumps({"agent_id": OTHER_ID, "approval": "waiting_for_person"})
            raise ds.ApiError(404, {})
        ds.http = fake
        ds.enroll()
        self.assertTrue(ds.is_private(other))
        self.assertFalse(ds.is_private(self.folder))

    def test_hello_records_and_clears_private_and_sends_no_causes_when_private(self):
        answers = [{"result": "hello_ok", "agent_id": OWN_ID, "causes": [], "private": True}]

        def fake(method, path, key=None, body=None, form=None, timeout=40, **kw):
            self.calls.append((method, path, key, body))
            return 200, "application/json", json.dumps(answers[0])
        ds.http = fake
        agent = self.agent()
        self.assertEqual(ds.send_hello(agent, "ldgr_ag_test", ["hook_disabled"])[0], "ok")
        self.assertTrue(ds.is_private(self.folder))
        self.assertEqual(self.calls[-1][3].get("blocked"), ["hook_disabled"])  # learned only from this answer
        ds.send_hello(agent, "ldgr_ag_test", ["hook_disabled"])
        self.assertNotIn("blocked", self.calls[-1][3])  # private now: the causes stay on this computer
        answers[0] = {"result": "hello_ok", "agent_id": OWN_ID, "causes": []}
        ds.send_hello(agent, "ldgr_ag_test")
        self.assertTrue(ds.is_private(self.folder))
        answers[0] = {"result": "hello_ok", "agent_id": OWN_ID, "causes": [], "private": False}
        ds.send_hello(agent, "ldgr_ag_test")
        self.assertFalse(ds.is_private(self.folder))


class TokenHelper(PrivateBase):
    def test_an_ordinary_folder_gets_the_login_token(self):
        ds.login_token = lambda force=False: "dsat_" + "p" * 40
        self.assertEqual(self.token(), {"Authorization": "Bearer dsat_" + "p" * 40})

    def test_a_private_folder_gets_the_agents_own_key(self):
        ds.login_token = lambda force=False: "dsat_" + "p" * 40
        self.make_private()
        ds.read_key = lambda s: "ldgr_ag_" + "k" * 40
        self.assertEqual(self.token(), {"Authorization": "Bearer ldgr_ag_" + "k" * 40})

    def test_a_private_folder_with_no_key_never_gets_the_login(self):
        ds.login_token = lambda force=False: "dsat_" + "p" * 40
        self.make_private()
        ds.read_key = lambda s: None
        self.assertEqual(self.token(), {"Authorization": f"Bearer {ds.SIGNED_OUT_BEARER}"})

    def test_the_hook_denies_the_connector_in_a_private_folder_when_the_helper_had_no_folder(self):
        ds.login_token = lambda force=False: "dsat_" + "p" * 40
        self.make_private()
        self.assertEqual(self.token(folder=None), {"Authorization": "Bearer dsat_" + "p" * 40})  # the plugin's own folder
        os.environ["CLAUDE_PROJECT_DIR"] = self.folder
        for name in ("mcp__plugin_datastash_datastash__ledger_inbox_send", "mcp__datastash__ledger_rows_get",
                     "mcp__claude_ai_Ledger__ledger_status_set"):
            out = self.tool(name)
            self.assertEqual(deny_of(out), "deny", name)
            self.assertIn("private", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertNotEqual(deny_of(self.tool("Read", tool_input={"file_path": "/etc/hostname"})), "deny")

    def test_the_connector_runs_once_the_helper_handed_this_sessions_folder_its_agent_key(self):
        self.make_private()
        self.token()
        self.assertNotEqual(deny_of(self.tool("mcp__plugin_datastash_datastash__ledger_rows_get")), "deny")
        # a claude.ai connector carries the person's login, never the agent key: still denied
        self.assertEqual(deny_of(self.tool("mcp__claude_ai_datastash__ledger_rows_get")), "deny")
        # another Claude Code process in the same folder did not get the key from its helper
        ds._claude_chain = lambda: [[5151, "start-B"]]
        self.assertEqual(deny_of(self.tool("mcp__plugin_datastash_datastash__ledger_rows_get")), "deny")

    def test_an_ordinary_folder_is_not_denied(self):
        ds.note_private({"agent_id": OWN_ID, "private": False}, OWN_ID)
        self.assertNotEqual(deny_of(self.tool("mcp__plugin_datastash_datastash__ledger_rows_get")), "deny")
        self.assertNotEqual(deny_of(self.tool("mcp__claude_ai_Ledger__ledger_rows_get")), "deny")


class StaysLocal(PrivateBase):
    def test_status_set_is_kept_here_and_never_sent(self):
        self.make_private()
        rc, out, err = self.run_cmd(ds.cmd_status_set, ["-"], stdin=json.dumps({"doing": "fixing the login bug"}))
        self.assertEqual(rc, 0, err)
        self.assertIn("private", out)
        self.assertEqual([a for a in self.spawned if "status-send" in a], [])
        self.assertIsNotNone(ds.read_json(ds._outbox_path("web-app"), None))
        # the shared local copy (other agents here read it) does not get it either
        c = ds.read_status_cache(WB) or {"agents": []}
        self.assertNotIn("fixing the login bug", json.dumps(c))
        seq = ds.read_json(ds._outbox_path("web-app"), {})["seq"]
        self.calls.clear()
        self.assertEqual(ds.cmd_status_send(["web-app", seq]), 0)
        self.assertEqual(self.calls, [])
        self.assertFalse(ds.resend_unsent("web-app"))

    def test_status_set_in_an_ordinary_folder_still_sends(self):
        rc, out, err = self.run_cmd(ds.cmd_status_set, ["-"], stdin=json.dumps({"doing": "fixing the login bug"}))
        self.assertEqual(rc, 0, err)
        self.assertTrue([a for a in self.spawned if "status-send" in a])

    def test_no_heartbeat_and_no_status_refresh_from_a_private_agent(self):
        agent = self.agent()
        self.assertTrue(ds.shares_from(agent))
        self.make_private()
        self.assertFalse(ds.shares_from(agent))
        self.calls.clear()
        stop = ds.threading.Event()
        live = {"key": "ldgr_ag_test"}
        orig = ds.BEAT_SECONDS
        try:
            ds.BEAT_SECONDS = 0.01
            t = ds.threading.Thread(target=ds._beat_loop, args=(agent, self.folder, live, stop), daemon=True)
            t.start()
            time.sleep(0.1)
            stop.set()
            t.join(2)
        finally:
            ds.BEAT_SECONDS = orig
        self.assertEqual([c for c in self.calls if c[1] == ds.HEARTBEAT_PATH], [])

    def test_do_k_for_a_feed_is_not_set_up_in_a_private_folder(self):
        self.make_private()
        inp = {"session_id": "sess1", "hook_event_name": "UserPromptSubmit", "cwd": self.folder, "prompt": "do 1"}
        self.assertEqual(ds.lookup_do(inp, 1, {"lookup": "github", "agent": "web-app"}), {"show": FEED_LINE})
        self.assertEqual(self.spawned, [])
        self.calls.clear()
        ds.lookup_setup({"service": "github", "agent": "web-app"}, "sess1")
        self.assertEqual(self.calls, [])

    def test_lookups_still_count_in_a_private_folder(self):
        self.make_private()
        inp = {"session_id": "sess1", "hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": self.folder,
               "tool_input": {"command": "gh pr list"}}
        ds.lookup_hook(inp)
        ds.lookup_hook(inp)
        self.assertTrue(ds.lookup_totals())

    def test_the_persons_list_shows_it_private(self):
        ds.write_private(ds.INSTALL_ASKED_PATH, json.dumps({"at": int(time.time()), "sid": "sess1"}))
        self.make_private()
        lines = ds.install_update_lines()
        self.assertIn("web-app: private", lines[0])


class MapStaysLocal(MapBase):
    def setUp(self):
        super().setUp()
        ds.write_private(ds.conf_path("web-app"), json.dumps({"name": "web-app", "agent_id": OWN_ID, "workbook_id": WB,
                                                              "projects": [self.folder]}))
        if os.path.exists(ds.PRIVATE_PATH):
            os.replace(ds.PRIVATE_PATH, os.path.join(TMP, f"old-private-{time.time()}"))
        self.agent = ds.all_agents()[0]

    def test_a_private_folder_maps_locally_and_shares_no_tab(self):
        ds.note_private({"agent_id": OWN_ID, "private": True}, OWN_ID)
        self.assertEqual(ds.map_refresh(self.agent, self.folder, "ldgr_ag_test"), "built")
        self.assertEqual(self.mcp_calls, [])
        self.assertEqual(self.reads, 0)
        self.assertEqual(self.local()["sha"], self.head)
        cwd = os.getcwd()
        os.chdir(self.folder)
        self.addCleanup(os.chdir, cwd)
        rc, out, err = self.run_cmd(ds.cmd_where, ["login", "check"])
        self.assertEqual(rc, 0, err)
        self.assertIn("src/auth.ts", out)

    def test_an_ordinary_folder_still_shares_its_tab(self):
        self.assertEqual(ds.map_refresh(self.agent, self.folder, "ldgr_ag_test"), "built")
        self.assertTrue(self.mcp_calls)


class LinkRefusals(PrivateBase):
    def test_link_refuses_a_private_agent_into_another_folder(self):
        self.make_private()
        other = self.other_folder("elsewhere")
        os.environ["CLAUDE_PROJECT_DIR"] = other
        rc, out, err = self.run_cmd(ds.cmd_link, ["web-app", "--workbook", WB])
        self.assertNotEqual(rc, 0)
        self.assertIn("private", err)
        self.assertNotIn(other, ds.read_json(ds.conf_path("web-app"), {})["projects"])

    def test_link_refuses_another_agent_into_a_private_folder(self):
        self.make_private()
        rc, out, err = self.run_cmd(ds.cmd_link, ["web-app-2", "--workbook", WB])
        self.assertNotEqual(rc, 0)
        self.assertIn("private", err)
        self.assertFalse(os.path.exists(ds.conf_path("web-app-2")))

    def test_link_and_auto_name_from_a_private_folder_refuse_another_folder(self):
        self.make_private()
        other = self.other_folder("elsewhere")
        ds.record_consent([other], [])
        rc, out, err = self.run_cmd(ds.cmd_link, ["helper", "--workbook", WB, "--project", other])
        self.assertNotEqual(rc, 0)
        self.assertIn("private", err)
        rc, out, err = self.run_cmd(ds.cmd_auto_name, ["--project", other])
        self.assertNotEqual(rc, 0)
        self.assertIn("private", err)

    def test_auto_name_refuses_a_private_agent_reused_in_another_folder(self):
        self.make_private()
        other = self.other_folder("elsewhere")
        orig = ds.session_for
        try:
            # this folder's session resolves to the private agent from elsewhere (the fallback "via" link)
            ds.session_for = lambda here, me=None: dict(self.agent(), via=True)
            os.environ["CLAUDE_PROJECT_DIR"] = other
            rc, out, err = self.run_cmd(ds.cmd_auto_name, [])
        finally:
            ds.session_for = orig
        self.assertNotEqual(rc, 0)
        self.assertIn("private", err)

    def test_auto_name_in_the_private_folder_reuses_its_agent(self):
        self.make_private()
        ds.signed_in = lambda: True
        rc, out, err = self.run_cmd(ds.cmd_auto_name, ["--no-login"])
        self.assertEqual(rc, 0, err)
        self.assertIn('linked: "web-app"', out)


if __name__ == "__main__":
    unittest.main()
