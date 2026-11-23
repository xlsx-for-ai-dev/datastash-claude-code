#!/usr/bin/env python3
"""Offline tests for XLI-328 card 1 (plugin 0.9.2): OFF folders done right, the install question's answers, and the
small leftovers.
- A folder the person left off ("leave X off", "keep X off datastash", "datastash off") makes no datastash call from
  then on: no session-start sign-in or count, no listener, no enroll, no hello, heartbeat, status, seed, map share or
  lookup counting; every datastash connector call there is denied by the PreToolUse hook (Claude Code runs the
  connector's headersHelper in the plugin's own folder, so the helper cannot tell the session's folder). Old 0.9.x
  "no" records read as off. Its agent is switched off on datastash with its own key; a refusal gets one plain line.
- "put X back on datastash" is checked as the person's own typed prompt.
- "add <folder>" joins a folder of this user through the plugin's own registration; the updated list is shown once.
- An answered own ask carries the server's status_answer into the agent's own view; another agent's ask never shows one.
- `gh api` with a write flag is not counted as a lookup.
- Every plugin git call blanks the command and hook settings, so a repo's own config runs nothing.
No network, no key; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_off.py"""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness  # noqa: E402
from test_status import Base, ds, WB, BIN, row, TMP  # noqa: E402  (the same harness, plugin copy and fixtures)

AGENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
REFUSED = "Couldn't switch it off on datastash; it is off on this computer."
WAITING = f"It is off on this computer. On {ds.AGENTS_URL}, press Don't allow next to it."


def mcp_answer(out, is_error=False):
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"isError": is_error,
                                                              "content": [{"type": "text", "text": json.dumps(out)}]}})


class OffBase(Base):
    def setUp(self):
        super().setUp()
        self.saved.update({k: getattr(ds, k) for k in ("recent_folders", "_login_id", "known_folders")
                           if hasattr(ds, k)})
        self.switch_answer = (False, {"name": "web-app", "switched_off": ["web-app"]})
        self.registered = []

        def fake_http(method, path, key=None, body=None, form=None, timeout=40, **kw):
            self.calls.append((method, path, key, body))
            if method == "POST" and path == ds.MCP_PATH:
                err, out = self.switch_answer
                if isinstance(out, Exception):
                    raise out
                return 200, "application/json", mcp_answer(out, err)
            if method == "POST" and path == ds.AGENT_REGISTER_PATH:
                self.registered.append(body)
                return 200, "application/json", json.dumps({"agent_id": AGENT_ID, "name": body["name"]})
            return 200, "application/json", json.dumps({"ok": True})
        ds.http = fake_http
        ds._login_id = lambda: "login1"
        ds.recent_folders = lambda *a, **k: []
        os.makedirs(ds.OFF, exist_ok=True)
        for n in os.listdir(ds.OFF):
            os.replace(os.path.join(ds.OFF, n), os.path.join(TMP, f"old-off-{n}-{time.time()}"))
        if os.path.exists(ds.ENROLL_FILE):
            os.replace(ds.ENROLL_FILE, os.path.join(TMP, f"old-enroll-{time.time()}"))
        for p in (ds.INSTALL_ASKED_PATH, ds.INSTALL_UPDATED_PATH):
            if os.path.exists(p):
                os.replace(p, os.path.join(TMP, f"old-install-{time.time()}"))

    def mcp_calls(self):
        return [c for c in self.calls if c[1] == ds.MCP_PATH]

    def tool(self, name, event="PreToolUse", tool_input=None, sid="sess1"):
        inp = {"session_id": sid, "hook_event_name": event, "tool_name": name, "tool_input": tool_input or {},
               "cwd": self.folder}
        return self.hook(ds.cmd_agent_tool, inp)

    def other_folder(self, name):
        f = os.path.join(harness.FAKE_HOME, "work", f"{name}-{uuid.uuid4().hex[:6]}", name)
        os.makedirs(f)
        return os.path.realpath(f)


class LeaveOff(OffBase):
    def test_leave_off_is_full_off_and_switches_the_agent_off_with_its_own_key(self):
        text = self.prompt("leave web-app off")
        self.assertEqual(self.last_hook["decision"], "block")
        self.assertIn('"web-app" is off datastash on this computer', text)
        self.assertTrue(ds.is_off(self.folder))
        self.assertFalse(ds.consent_for(self.folder))
        calls = self.mcp_calls()
        self.assertEqual(len(calls), 1)
        _m, _p, key, body = calls[0]
        self.assertEqual(key, "ldgr_ag_test")  # the agent's own key (read_key), never the person's login
        self.assertEqual(body["params"], {"name": "ledger_agent_switch_off", "arguments": {"name": "web-app", "action": "off"}})
        self.assertIn("switched off on datastash too", text)
        self.assertNotIn(REFUSED, text)
        # its key and config moved to the off folder; nothing deleted
        self.assertFalse(os.path.exists(ds.key_path("web-app")))
        self.assertFalse(os.path.exists(ds.conf_path("web-app")))
        self.assertEqual(sorted(n.split(".json")[0].split(".key")[0] for n in os.listdir(ds.OFF)),
                         sorted([os.path.basename(ds.conf_path("web-app")).split(".json")[0],
                                 os.path.basename(ds.key_path("web-app")).split(".key")[0]]))

    def test_a_refused_switch_off_gets_the_plain_line(self):
        self.switch_answer = (True, {"code": "not_allowed", "message": 'Only the person\'s connection or an agent above "web-app" can switch it off.'})
        text = self.prompt("leave web-app off")
        self.assertIn(REFUSED, text)
        self.assertNotIn("not_allowed", text)
        self.assertTrue(ds.is_off(self.folder))

    def test_an_unreachable_datastash_gets_the_plain_line(self):
        self.switch_answer = (False, ds.ApiError(503, {"code": "busy"}))
        text = self.prompt("leave web-app off")
        self.assertIn(REFUSED, text)
        self.assertTrue(ds.is_off(self.folder))

    def test_a_waiting_agent_gets_the_dont_allow_line(self):
        other = self.other_folder("docs")
        ds._set_enrolled(other, {"name": "docs", "agent_id": AGENT_ID, "proof": "p" * 43, "login": "login1", "at": 1})
        text = self.prompt("leave docs off")
        self.assertIn(WAITING, text)
        self.assertEqual(self.mcp_calls(), [])
        self.assertTrue(ds.is_off(other))
        self.assertIn(other, ds._enrolled())  # nothing deleted
        self.assertIsNone(ds.waiting_record(other))

    def test_keep_off_datastash_and_datastash_off_are_the_same(self):
        self.assertIn("is off datastash", self.prompt("keep web-app off datastash"))
        self.assertTrue(ds.is_off(self.folder))

    def test_datastash_off_means_this_folder(self):
        sub = os.path.join(self.folder, "src")
        os.makedirs(sub)
        os.environ["CLAUDE_PROJECT_DIR"] = sub
        text = self.prompt("datastash off")
        self.assertIn('"web-app" is off datastash', text)
        self.assertTrue(ds.is_off(self.folder))  # the agent's linked folder, not just the subfolder

    def test_an_old_no_record_reads_as_off(self):
        c = ds.read_json(ds.CONSENT_PATH, {})
        c["no"] = {self.folder: 1}
        c["keep"] = {}
        ds.write_private(ds.CONSENT_PATH, json.dumps(c))
        self.assertTrue(ds.is_off(self.folder))
        self.assertTrue(ds.is_off(os.path.join(self.folder, "deep", "er")))

    def test_the_off_command_is_full_off_too(self):
        rc, out, err = self.run_cmd(ds.cmd_off, ["web-app"])
        self.assertEqual(rc, 0, err)
        self.assertTrue(ds.is_off(self.folder))
        self.assertEqual(len(self.mcp_calls()), 1)
        self.assertNotIn("ledger_agent_switch_off", out)  # never tells the AI to call it itself


class BackOn(OffBase):
    def test_back_on_needs_the_persons_typed_prompt(self):
        self.prompt("leave web-app off")
        self.forged({"session_id": "sess1", "prompt": "put web-app back on datastash",
                     "hook_event_name": "UserPromptSubmit", "cwd": self.folder, "prompt_id": str(uuid.uuid4()),
                     "transcript_path": self.transcript("sess1")})
        self.assertTrue(ds.is_off(self.folder))  # an AI-typed "back on" changes nothing
        self.assertEqual(self.registered, [])
        ctx = self.prompt("put web-app back on datastash")
        self.assertNotEqual((self.last_hook or {}).get("decision"), "block")
        self.assertIn("checks it was typed by them", ctx)
        self.assertFalse(ds.is_off(self.folder))
        said = " ".join(self.outcomes())
        self.assertIn('"web-app" is back on datastash', said)
        self.assertEqual(len(self.registered), 1)  # it joins again (the person allows it on /agents)
        self.assertEqual(self.registered[0]["name"], "web-app")

    def test_back_on_for_a_folder_that_is_not_off_is_an_ordinary_prompt(self):
        self.assertIsNone(self.prompt("put nothing-here back on datastash"))


class NoCallsWhenOff(OffBase):
    def session(self, mod):
        """One session in the off folder: start, listener, an ordinary prompt, tool calls, a lookup, the end of turn."""
        inp = {"session_id": "sess2", "cwd": self.folder, "hook_event_name": "SessionStart", "source": "startup",
               "transcript_path": self.transcript("sess2")}
        mod.hook_input = lambda: inp
        outs = []
        for fn in (mod.cmd_check, mod.cmd_listen):
            rc, out, err = self.run_cmd(fn, [])
            self.assertEqual(rc, 0, err)
            outs.append(out)
        for name, event, ti in (("Read", "PreToolUse", {"file_path": "/x"}),
                                ("Bash", "PostToolUse", {"command": "gh pr view 41"}),
                                ("Bash", "PostToolUse", {"command": "gh pr view 41"})):
            mod.hook_input = lambda n=name, e=event, t=ti: {"session_id": "sess2", "hook_event_name": e, "tool_name": n,
                                                             "tool_input": t, "cwd": self.folder}
            self.run_cmd(mod.cmd_agent_tool, [])
        mod.hook_input = lambda: {"session_id": "sess2", "prompt": "fix the login bug", "hook_event_name": "UserPromptSubmit",
                                  "cwd": self.folder, "prompt_id": str(uuid.uuid4()), "transcript_path": self.transcript("sess2")}
        self.run_cmd(mod.cmd_prompt, [])
        mod.hook_input = lambda: {"session_id": "sess2", "hook_event_name": "Stop", "cwd": self.folder,
                                  "transcript_path": self.transcript("sess2")}
        self.run_cmd(mod.cmd_status_check, [])
        return outs

    def test_an_off_folder_makes_zero_datastash_calls_across_a_restart_and_an_update(self):
        ds.recent_folders = lambda *a, **k: [self.folder]
        self.prompt("leave web-app off")
        self.calls.clear()
        self.spawned.clear()
        outs = self.session(ds)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.spawned, [])
        self.assertIn("datastash is off in this folder", outs[0])
        self.assertEqual(ds.lookup_totals(), {})  # no lookup counted from an off folder
        # a plugin update: a fresh copy of the plugin, the same config folder
        loader = importlib.machinery.SourceFileLoader("datastash_updated", BIN)
        spec = importlib.util.spec_from_loader("datastash_updated", loader)
        ds2 = importlib.util.module_from_spec(spec)
        loader.exec_module(ds2)
        fresh = []
        ds2.http = lambda *a, **k: fresh.append(a) or (_ for _ in ()).throw(AssertionError("a datastash call"))
        ds2.signed_in = lambda: True
        ds2.login_token = lambda force=False: fresh.append("login") or "dsat_person"
        ds2._login_id = lambda: "login1"
        ds2.recent_folders = lambda *a, **k: [self.folder]
        ds2._claude_chain = lambda: [[4242, "start-A"]]
        ds2.spawn_detached = lambda argv, cwd: fresh.append(argv)
        self.session(ds2)
        self.assertEqual(fresh, [])

    def test_every_datastash_connector_call_is_denied_in_an_off_folder(self):
        self.prompt("leave web-app off")
        for name in ("mcp__plugin_datastash_datastash__ledger_rows_get", "mcp__plugin_datastash_datastash__ledger_person_watch",
                     "mcp__datastash__ledger_inbox_send", "mcp__claude_ai_datastash__ledger_agent_register"):
            out = self.tool(name, tool_input={"action": "main"})
            hs = (out or {}).get("hookSpecificOutput") or {}
            self.assertEqual(hs.get("permissionDecision"), "deny", name)
            self.assertIn("off in this folder", hs.get("permissionDecisionReason", ""))
        out = self.tool("Read", tool_input={"file_path": "/etc/hostname"})
        self.assertNotEqual(((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision"), "deny")

    def test_a_renamed_claude_ai_connector_is_denied_in_an_off_folder(self):
        self.prompt("leave web-app off")
        for name in ("mcp__claude_ai_Ledger__ledger_rows_get", "mcp__claude_ai_My_sheets__ledger_inbox_send"):
            hs = (self.tool(name) or {}).get("hookSpecificOutput") or {}
            self.assertEqual(hs.get("permissionDecision"), "deny", name)
        hs = (self.tool("mcp__claude_ai_Other__rows_get") or {}).get("hookSpecificOutput") or {}
        self.assertNotEqual(hs.get("permissionDecision"), "deny")

    def test_a_ledger_tool_in_an_on_folder_is_not_denied(self):
        hs = (self.tool("mcp__claude_ai_Ledger__ledger_rows_get") or {}).get("hookSpecificOutput") or {}
        self.assertNotEqual(hs.get("permissionDecision"), "deny")

    def test_a_folder_that_is_on_is_not_denied(self):
        out = self.tool("mcp__plugin_datastash_datastash__ledger_rows_get")
        self.assertNotEqual(((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision"), "deny")

    def test_an_off_folder_is_not_enrolled(self):
        other = self.other_folder("api")
        off = self.other_folder("secret-work")
        ds.recent_folders = lambda *a, **k: [off, other]
        ds.record_left_off([off])
        ds.enroll()
        self.assertEqual([b["name"] for b in self.registered], ["api"])
        self.assertNotIn(off, ds._enrolled())

    def test_auto_name_and_link_refuse_an_off_folder_the_current_one_included(self):
        ds.record_left_off([self.folder])
        rc, out, err = self.run_cmd(ds.cmd_auto_name, [])
        self.assertNotEqual(rc, 0)
        self.assertIn("off", err + out)
        self.assertNotIn("name:", out)
        rc, out, err = self.run_cmd(ds.cmd_link, ["web-app-2", "--workbook", WB])
        self.assertNotEqual(rc, 0)
        self.assertIn("off", err)
        other = self.other_folder("elsewhere")
        os.environ["CLAUDE_PROJECT_DIR"] = other
        rc, out, err = self.run_cmd(ds.cmd_link, ["web-app-2", "--workbook", WB, "--project", self.folder])
        self.assertNotEqual(rc, 0)

    def test_doctor_shows_the_folder_off_and_calls_nothing(self):
        self.prompt("leave web-app off")
        self.calls.clear()
        rc, out, err = self.run_cmd(ds.cmd_doctor, [])
        self.assertEqual(rc, 0, err)
        self.assertIn("off: this folder is off datastash on this computer (web-app)", out)
        self.assertEqual(self.calls, [])

    def test_no_seed_and_no_agent_in_an_off_folder(self):
        ds.record_left_off([self.folder])
        self.assertIsNone(ds.agent_for(self.folder))
        self.assertEqual(ds.own_status_missing(self.folder, None), (None, False))
        self.assertIsNone(ds.listen_target(self.folder))


class InstallAnswers(OffBase):
    def shown_install(self, sid="sess1"):
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        self.calls.clear()
        text = self.prompt("status of all my agents", sid=sid)
        self.assertTrue(text.rstrip().endswith(ds.INSTALL_QUESTION))

    def test_add_a_folder_joins_now_through_the_plugin(self):
        self.shown_install()
        api = self.other_folder("billing-api")
        ds.known_folders = lambda: [api]
        ctx = self.prompt("add billing-api")
        self.assertIn("checks it was typed by them", ctx)
        self.assertEqual([b["name"] for b in self.registered], ["billing-api"])
        self.assertIn(api, ds._enrolled())
        said = " ".join(self.outcomes())
        self.assertIn('"billing-api" joins datastash now', said)
        self.assertIn("Your agents on datastash now:", said)  # the updated list, once

    def test_add_by_ai_alone_registers_nothing(self):
        self.shown_install()
        api = self.other_folder("billing-api")
        ds.known_folders = lambda: [api]
        self.forged({"session_id": "sess1", "prompt": "add billing-api", "hook_event_name": "UserPromptSubmit",
                     "cwd": self.folder, "prompt_id": str(uuid.uuid4()), "transcript_path": self.transcript("sess1")})
        self.assertEqual(self.registered, [])

    def test_add_a_name_not_found_here_passes_through(self):
        self.shown_install()
        ds.known_folders = lambda: []
        self.assertIsNone(self.prompt("add mobile-app"))
        self.assertNotEqual((self.last_hook or {}).get("decision"), "block")
        self.assertEqual(self.registered, [])

    def test_an_ordinary_add_prompt_right_after_the_question_is_untouched(self):
        self.shown_install()
        ds.known_folders = lambda: [self.other_folder("billing-api"), self.other_folder("settings")]
        for p in ("add dark mode to the settings page", "add tests", "add error handling.",
                  "add dark mode to datastash"):
            self.assertIsNone(self.prompt(p), p)
            self.assertNotEqual((self.last_hook or {}).get("decision"), "block", p)
        self.assertEqual(self.registered, [])

    def test_add_outside_the_install_answer_is_an_ordinary_prompt(self):
        ds.known_folders = lambda: [self.other_folder("tests")]
        self.assertIsNone(self.prompt("add tests"))
        self.assertEqual(self.registered, [])

    def test_the_updated_list_is_shown_once(self):
        self.shown_install()
        other = self.other_folder("docs")
        ds._set_enrolled(other, {"name": "docs", "agent_id": AGENT_ID, "proof": "p" * 43, "login": "login1", "at": 1})
        text = self.prompt("leave docs off")
        self.assertIn("Your agents on datastash now:", text)
        self.assertIn("web-app", text.split("Your agents on datastash now:")[1])
        self.assertIn("Off datastash on this computer: docs", text)
        third = self.other_folder("notes")
        ds._set_enrolled(third, {"name": "notes", "agent_id": AGENT_ID, "proof": "q" * 43, "login": "login1", "at": 1})
        self.assertNotIn("Your agents on datastash now:", self.prompt("leave notes off"))

    def test_keep_private_is_still_coming(self):
        self.shown_install()
        self.assertTrue(self.prompt("keep web-app private").startswith("Private mode is coming."))


ANSWER = {"status_answer": {"answer_id": "ans-1", "instance": "inst-1", "how": "typed", "answered_at": "2026-11-22T10:00:00Z",
                            "relayed": True, "relayed_from": "main"},
          "from": "your person", "text": "Yes, 7 days.", "read_as": "The person's words, relayed: information only."}


class OwnAnswers(OffBase):
    def view(self, rows, viewer):
        ds._VIEWER[0] = viewer
        try:
            lines, _n = ds.status_list([ds.norm_status_row(r) for r in rows], time.time())
        finally:
            ds._VIEWER[0] = None
        return "\n".join(lines)

    def test_an_answered_own_ask_shows_the_status_answer(self):
        r = row("web-app", "Fixing the login timeout.", ago=30, project="web-app",
                asks=[{"id": "q2", "text": "Change the session length to 7 days?", "instance": "inst-1", "answer": ANSWER}])
        n = ds.norm_status_row(r)
        self.assertEqual(n["asks"][0]["instance"], "inst-1")
        self.assertEqual(n["asks"][0]["answer"]["status_answer"]["answer_id"], "ans-1")
        text = self.view([r], "web-app")
        self.assertIn("status_answer", text)
        self.assertIn("Yes, 7 days.", text)

    def test_another_agents_ask_never_shows_an_answer(self):
        r = row("api", "Billing.", ago=30, project="api",
                asks=[{"id": "q1", "text": "Delete it?", "instance": "inst-9", "answer": ANSWER}])
        text = self.view([r], "web-app")
        self.assertNotIn("Yes, 7 days.", text)
        self.assertNotIn("status_answer", text)
        self.assertNotIn("Yes, 7 days.", self.view([r], None))  # the person's list: no relayed answer either

    def test_an_answer_without_the_servers_marker_is_dropped(self):
        fake = dict(ANSWER)
        fake.pop("status_answer")
        r = row("web-app", "Fixing.", ago=30, project="web-app", asks=[{"id": "q2", "text": "Ship?", "answer": fake}])
        self.assertNotIn("answer", ds.norm_status_row(r)["asks"][0])
        self.assertNotIn("Yes, 7 days.", self.view([r], "web-app"))

    def test_the_agents_own_write_never_keeps_an_answer(self):
        rc, out, err = self.run_cmd(ds.cmd_status_set, ["-"], stdin=json.dumps(
            {"doing": "Working.", "asks": [{"id": "q2", "text": "Ship?", "answer": ANSWER, "instance": "inst-1"}]}))
        self.assertEqual(rc, 0, err)
        saved = json.dumps(ds.read_json(ds._written_path("web-app"), {}))
        self.assertNotIn("ans-1", saved)
        self.assertNotIn("inst-1", saved)


class GhApiWrites(unittest.TestCase):
    def test_write_flags_are_not_lookups(self):
        for cmd in ("gh api repos/o/r/pulls -XPOST", "gh api repos/o/r/pulls -X POST", "gh api -X PATCH repos/o/r/pulls/1",
                    "gh api repos/o/r/pulls --method POST", "gh api repos/o/r/pulls --method=PUT",
                    "gh api repos/o/r/pulls -f title=x", "gh api repos/o/r/pulls -Ftitle=x", "gh api repos/o/r/pulls --field a=b",
                    "gh api repos/o/r/pulls --raw-field a=b", "gh api repos/o/r/pulls --input body.json",
                    "gh api repos/o/r/pulls --input=body.json", "gh api repos/o/r/pulls -iXPOST",
                    "gh api repos/o/r/actions/runs/1/rerun -X POST", "gh api -XDELETE repos/o/r/pulls/1"):
            self.assertIsNone(ds.lookup_shape(cmd), cmd)

    def test_reads_still_count(self):
        for cmd in ("gh api repos/o/r/pulls", "gh api repos/o/r/pulls -X GET", "gh api -i repos/o/r/pulls",
                    "gh api repos/o/r/pulls --method=get", "gh api repos/o/r/actions/runs --paginate -q .x"):
            self.assertEqual(ds.lookup_shape(cmd), ("github", "gh api " + ("actions" if "actions" in cmd else "pulls")), cmd)


class GitBlanks(Base):
    def make_repo(self):
        root = os.path.realpath(tempfile.mkdtemp(prefix="repo-", dir=TMP))
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1")
        run = lambda *a: subprocess.run(["git", "-C", root] + list(a), check=True, capture_output=True, env=env)
        run("init", "-q")
        run("config", "user.email", "t@example.invalid")
        run("config", "user.name", "t")
        with open(os.path.join(root, "a.py"), "w") as f:
            f.write("def x():\n    pass\n")
        run("add", "a.py")
        run("commit", "-qm", "one")
        pwn = os.path.join(root, "..", os.path.basename(root) + "-pwn")
        hook = os.path.join(root, "..", os.path.basename(root) + "-hook.sh")
        with open(hook, "w") as f:
            f.write(f"#!/bin/sh\ntouch '{pwn}'\n")
        os.chmod(hook, 0o700)
        for k in ("core.fsmonitor", "core.alternateRefsCommand", "core.sshCommand", "diff.external", "core.pager"):
            run("config", k, hook)
        run("config", "core.hooksPath", os.path.dirname(hook))
        return root, os.path.realpath(pwn)

    def test_a_repos_own_config_runs_nothing_during_a_map_refresh(self):
        root, pwn = self.make_repo()
        sha = ds._git(root, "rev-parse", "HEAD")
        self.assertRegex(sha, r"^[0-9a-f]{40}$")
        ds.git_context(root)
        ds.map_build(root, sha)
        self.assertFalse(os.path.exists(pwn))
        # the same repo without the blanks does run it (so the test proves something)
        subprocess.run(["git", "-C", root, "ls-files", "-ci", "--exclude-standard"], capture_output=True)
        subprocess.run(["git", "-C", root, "status", "--porcelain"], capture_output=True)
        self.assertTrue(os.path.exists(pwn))

    def test_a_repo_set_to_show_signatures_runs_no_gpg_program(self):
        root, pwn = self.make_repo()
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1")
        run = lambda *a, **k: subprocess.run(["git", "-C", root] + list(a), check=True, capture_output=True, env=env, **k)
        head = run("cat-file", "commit", "HEAD").stdout.decode()
        hdr, body = head.split("\n\n", 1)
        sig = "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEz\n -----END PGP SIGNATURE-----"
        obj = f"{hdr}\n{sig}\n\n{body}"
        sha = run("hash-object", "-t", "commit", "-w", "--stdin", input=obj.encode()).stdout.decode().strip()
        run("update-ref", "HEAD", sha)
        hook = os.path.join(root, "..", os.path.basename(root) + "-hook.sh")
        run("config", "log.showSignature", "true")
        for k in ("gpg.program", "gpg.ssh.program", "gpg.x509.program"):
            run("config", k, hook)
        self.assertEqual(ds.git_context(root)["last_commit"], "one")
        self.assertEqual(ds._git(root, "rev-parse", "HEAD"), sha)
        self.assertFalse(os.path.exists(pwn))
        # the same repo without the blanks does run it (so the test proves something)
        subprocess.run(["git", "-C", root, "log", "-1", "--format=%s"], capture_output=True, env=env)
        self.assertTrue(os.path.exists(pwn))

    def test_a_log_call_takes_no_diff_driver(self):
        argv, _env = ds.git_argv("/x", ["log", "-1", "--format=%s"])
        i = argv.index("log")
        self.assertEqual(argv[i:], ["log", "--no-ext-diff", "--no-textconv", "-1", "--format=%s"])
        self.assertIn("log.showSignature=false", argv)

    def test_every_git_call_goes_through_the_one_helper(self):
        with open(BIN) as f:
            src = f.read()
        self.assertEqual(src.count('["git"'), 1, "a git call outside git_argv")
        argv, env = ds.git_argv("/x", ["rev-parse", "HEAD"])
        for blank in ("core.fsmonitor=", "core.hooksPath=/dev/null", "core.alternateRefsCommand=", "core.sshCommand=",
                      "credential.helper=", "diff.external=", "core.pager=cat", "log.showSignature=false",
                      "pager.log=false", "pager.show=false"):
            self.assertIn(blank, argv)
        self.assertEqual((env["GIT_CONFIG_NOSYSTEM"], env["GIT_TERMINAL_PROMPT"]), ("1", "0"))
        with self.assertRaises(ValueError):
            ds.git_argv("/x", ["fetch"])


class TokenHelperDeny(OffBase):
    """XLI-310 (EPM M2 on a69057f): the login token goes only to the connector's headersHelper. A Bash tool call that runs
    the token helper, or a tool call that reads the login's token files, is denied by the PreToolUse hook; the reason
    never carries a token."""

    TOKEN = "dsat_" + "z" * 43

    def setUp(self):
        super().setUp()
        ds.write_private(ds.LOGIN_ACCESS_FILE, json.dumps({"token": self.TOKEN, "exp": int(time.time()) + 900}))

    def denied(self, out):
        hs = (out or {}).get("hookSpecificOutput", {})
        return hs.get("permissionDecision") == "deny"

    def test_a_bash_call_that_runs_the_token_helper_is_denied(self):
        for cmd in ('python3 "$CLAUDE_PLUGIN_ROOT/scripts/datastash" token --header | cat',
                    "datastash token --header",
                    "datastash token --header > /tmp/t; cat /tmp/t",
                    "python3 '/home/p/.claude/plugins/cache/datastash/scripts/datastash' 'token' '--header'",
                    "CLAUDE_CODE_MCP_SERVER_URL=https://getdatastash.com/mcp datastash token --header",
                    "exec datastash token --header",
                    "cd /x && datastash token"):
            with self.subTest(cmd=cmd):
                out = self.tool("Bash", tool_input={"command": cmd})
                self.assertTrue(self.denied(out), out)
                text = json.dumps(out)
                self.assertNotIn(self.TOKEN, text)
                self.assertNotIn("dsat_", text)
                self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"], ds.TOKEN_DENY)

    def test_a_tool_call_that_reads_the_login_token_files_is_denied(self):
        for name, ti in (("Bash", {"command": f"cat {ds.LOGIN_ACCESS_FILE}"}),
                         ("Bash", {"command": "cat ~/.config/datastash/login.refresh"}),
                         ("Read", {"file_path": ds.LOGIN_ACCESS_FILE}),
                         ("Grep", {"pattern": "dsat_", "path": os.path.join(ds.CONF, "login.access.json")})):
            with self.subTest(name=name, ti=ti):
                out = self.tool(name, tool_input=ti)
                self.assertTrue(self.denied(out), out)
                self.assertNotIn(self.TOKEN, json.dumps(out))

    def test_other_datastash_commands_and_other_tools_are_not_denied(self):
        for name, ti in (("Bash", {"command": "datastash status"}), ("Bash", {"command": "datastash login --check"}),
                         ("Bash", {"command": "git log --oneline -3"}), ("Bash", {"command": "echo token"}),
                         ("Read", {"file_path": "/x/README.md"})):
            with self.subTest(name=name, ti=ti):
                self.assertFalse(self.denied(self.tool(name, tool_input=ti)))

    def test_only_before_the_call(self):
        cmd = {"command": "datastash token --header"}
        for event in ("PostToolUse", "PostToolUseFailure"):
            self.assertFalse(self.denied(self.tool("Bash", event=event, tool_input=cmd)))

    def test_the_call_still_marks_the_session_as_an_agent(self):
        self.tool("Bash", tool_input={"command": "datastash token --header"}, sid="sess-tok")
        self.assertTrue(os.path.exists(os.path.join(ds.STATE, "agent-used-sid-sess-tok")))


if __name__ == "__main__":
    unittest.main()
