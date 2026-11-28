#!/usr/bin/env python3
"""Offline tests for XLI-436 (plugin 1.0.38): coordination gaps from MS's review (#9, #11, #13, #14).
- #9: with clean sessions on, the main session claims (ledger_agent_job, `datastash api POST /api/v1/agent/job`) the
  same way it hands off and writes its status.
- #11: a new agent is never named after the computer's OS user (the home folder or "/" asks for a typed name); the status
  list shows a known project name instead of "?", and one agent's rows with nothing open under the same (or no) project
  name collapse into one. Only the view changes: no row's own project is rewritten, nothing is deleted.
- #13: the GitHub lookups offer says plainly that a yes adds a webhook to the repo (a change to its settings).
- #14: the Setup tab's "full-DDS missing" column also names the claims, hand-offs, check-backs and remember an agent has
  not used (checkin's usage.this_agent and the Team tab); it never reads "none" while they are unused or not known.
No network, no key; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_xli436.py"""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import Base, ds, row, NOW, WB  # noqa: E402  (the same harness, plugin copy and fixtures)
from test_clean import bash, denied, DS_MCP  # noqa: E402

AGENT = {"slug": "web-app", "name": "web-app", "workbook_id": WB}


class ClaimsInCleanSessions(Base):
    """#9: claims run in the main session exactly as hand-offs do."""
    def call(self, tool, ti):
        return self.hook(ds.cmd_agent_tool, bash(tool=tool, tool_input=ti, cwd=self.folder))

    def test_claim_renew_release_and_list_run_like_a_hand_off(self):
        for no_code in (False, True):
            ds.set_clean(commands=True, no_code=no_code)
            for action in ("claim", "renew", "release", "list"):
                for server in (DS_MCP, "mcp__claude_ai_datastash__"):
                    ti = {"workbook_id": WB, "action": action, "job": "XLI-12", "fence": 1}
                    self.assertFalse(denied(self.call(server + "ledger_agent_job", ti)), (server, action))
                    self.assertFalse(denied(self.call(server + "ledger_agent_handoff", dict(ti, job=None))), (server, action))
                body = json.dumps({"workbook_id": WB, "action": action, "job": "XLI-12"})
                for path in ("/api/v1/agent/job", "/api/v1/agent/handoff"):
                    cmd = f"datastash api POST {path} '{body}'"
                    self.assertTrue(ds.clean_coord_ok(cmd), cmd)
                    self.assertFalse(denied(self.call("Bash", {"command": cmd})), cmd)
                    piped = f"printf '%s' '{body}' | datastash api POST {path} -"
                    self.assertTrue(ds.clean_coord_ok(piped), piped)

    def test_only_the_claim_door_itself_runs(self):
        ds.set_clean(commands=True, no_code=False)
        for cmd in ("datastash api POST '/api/v1/agent/job?x=1' '{}'", "datastash api POST /api/v1/agent/job/ '{}'",
                    "datastash api POST /api/v1/agent/jobs '{}'", "datastash api POST /api/v1/agent/job @f.json",
                    "datastash api POST /api/v1/agent/job '{}' ; touch F", "datastash api DELETE /api/v1/agent/job '{}'"):
            self.assertFalse(ds.clean_coord_ok(cmd), cmd)
            self.assertTrue(denied(self.call("Bash", {"command": cmd})), cmd)
        self.assertTrue(denied(self.call("mcp__evil__ledger_agent_jobx", {})))
        self.assertTrue(denied(self.call(DS_MCP + "ledger_agent_result", {"workbook_id": WB})))  # results stay refused


class NeverTheOsUser(Base):
    """#11: no new agent named after the computer's user; the folder's or the person's typed name instead."""
    def setUp(self):
        super().setUp()
        self.home_saved = ds.HOME
        self.addCleanup(setattr, ds, "HOME", self.home_saved)
        self.home = os.path.join(os.path.dirname(self.folder), "osuser")
        os.makedirs(self.home, exist_ok=True)
        ds.HOME = self.home
        self.user = mock.patch("getpass.getuser", return_value="osuser")
        self.user.start()
        self.addCleanup(self.user.stop)

    def test_the_home_folder_and_root_ask_for_a_name(self):
        for here in (self.home, "/"):
            self.assertEqual(ds.auto_name(here, None), ("ask", None), here)
            self.assertIsNone(ds._free_name(here), here)

    def test_a_typed_or_session_name_is_used_in_the_home_folder(self):
        self.assertEqual(ds.auto_name(self.home, None, session_name="spm"), ("name", "spm"))
        self.assertEqual(ds._free_name(self.home, prefer="SPM"), "spm")

    def test_a_folder_takes_its_own_name_and_a_nameless_one_asks(self):
        web = os.path.join(os.path.dirname(self.folder), "shop-site")
        os.makedirs(web, exist_ok=True)
        self.assertEqual(ds.auto_name(web, None), ("name", "shop-site"))
        dots = os.path.join(os.path.dirname(self.folder), "d436", "...")
        os.makedirs(dots, exist_ok=True)
        self.assertEqual(ds.auto_name(dots, None), ("ask", None))  # never "osuser"

    def test_the_home_install_asks_and_the_sign_in_code_names_no_agent(self):
        inp = {"session_id": "s436", "cwd": self.home, "hook_event_name": "UserPromptSubmit"}
        with mock.patch.object(ds, "project_of", return_value=self.home):
            kind, name = ds.auto_name(ds.project_of(inp), None, session_name=ds._session_name(inp))
        self.assertEqual((kind, name), ("ask", None))
        self.assertNotEqual(name, "osuser")


def nrow(agent, doing, **kw):
    return ds.norm_status_row(row(agent, doing, **kw))


class CleanStatusList(Base):
    """#11: a known project name instead of "?", one row for duplicates with nothing open; nothing deleted."""
    def test_a_row_with_no_project_shows_the_agents_one_project_and_collapses_into_it(self):
        rows = [nrow("helploom-live", "Shipping the help page.", project="helploom-live", ago=60),
                nrow("helploom-live", "Old status.", ago=7200)]
        lines, numbers = ds.status_list(rows, NOW)
        text = "\n".join(lines)
        self.assertNotIn("?", text.replace("**", ""))
        self.assertNotIn("on 2 projects", text)
        self.assertIn("Shipping the help page", text)
        self.assertEqual(sum(1 for x in lines if "helploom-live" in x and x[:1].isdigit()), 1)
        self.assertEqual([v["project"] for v in numbers.values()], ["helploom-live"])

    def test_duplicate_rows_with_no_project_collapse_to_the_newest(self):
        rows = [nrow("EPM", "Newest words.", ago=60), nrow("epm", "Older words.", ago=4000)]
        lines, numbers = ds.status_list(rows, NOW)
        text = "\n".join(lines)
        self.assertIn("Newest words.", text)
        self.assertNotIn("Older words.", text)
        self.assertNotIn("?", text.replace("**", ""))
        self.assertEqual(len(numbers), 1)

    def test_a_row_with_an_ask_is_never_collapsed_and_keeps_its_own_project_for_the_answer(self):
        ask = {"id": "q9", "version": 1, "text": "Ship it?"}
        rows = [nrow("helploom-live", "Building.", project="helploom-live", ago=60,
                     cards=[{"id": "XLI-9", "step": "build"}]),
                nrow("helploom-live", "Waiting on you.", asks=[ask], ago=600)]
        lines, numbers = ds.status_list(rows, NOW)
        text = "\n".join(lines)
        self.assertIn("Ship it?", text)
        self.assertIn("Building.", text)
        self.assertIn("on 2 projects: helploom-live, helploom-live", text)
        asked = [v for v in numbers.values() if v.get("ask") == "q9"]
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0]["project"], "")  # the answer still goes to that row's own key
        self.assertEqual(rows[1]["project"], "")  # never rewritten

    def test_this_computers_linked_folder_names_an_unnamed_row(self):
        rows = [nrow("web-app", "One.", project="api", ago=60, cards=[{"id": "XLI-1", "step": "build"}]),
                nrow("web-app", "Two.", ago=90, cards=[{"id": "XLI-2", "step": "build"}])]
        text = "\n".join(ds.status_list(rows, NOW)[0])
        self.assertIn(f"on 2 projects: api, {os.path.basename(self.folder)}", text)

    def test_no_known_name_says_so_never_a_question_mark(self):
        rows = [nrow("ops", "A.", project="a", ago=60, cards=[{"id": "XLI-1", "step": "build"}]),
                nrow("ops", "B.", project="b", ago=60, cards=[{"id": "XLI-2", "step": "build"}]),
                nrow("ops", "C.", ago=60, cards=[{"id": "XLI-3", "step": "build"}])]
        text = "\n".join(ds.status_list(rows, NOW)[0])
        self.assertIn("on 3 projects: a, b, no project name", text)
        self.assertNotIn("?", text.replace("**", ""))

    def test_different_agents_and_named_projects_are_unchanged(self):
        rows = [nrow("api", "Billing.", project="billing", ago=60, cards=[{"id": "XLI-1", "step": "build"}]),
                nrow("api", "Invoices.", project="invoices", ago=90, cards=[{"id": "XLI-2", "step": "build"}]),
                nrow("docs", "Docs.", project="docs", ago=60)]
        lines, numbers = ds.status_list(rows, NOW)
        self.assertEqual(lines[0], "**api** — on 2 projects: billing, invoices")
        self.assertEqual(sorted(v["agent"] for v in numbers.values()), ["api", "api", "docs"])


class LookupsOfferSaysTheChange(unittest.TestCase):
    """#13: the offer says the yes adds a webhook (a change to the repo's settings)."""
    def test_each_feed_service_says_its_webhook(self):
        self.assertEqual(ds.LOOKUP_CHANGE["github"], "Saying yes adds a webhook to the repo, which is a change to that "
                                                     "repo's settings.")
        for svc in ds.FEED_SERVICES:
            self.assertIn("webhook", ds.LOOKUP_CHANGE[svc])
            self.assertIn("settings", ds.LOOKUP_CHANGE[svc])


class HonestAdoption(Base):
    """#14: the column names the unused habits from the usage counter and the Team tab."""
    def check(self, tools, facts=(), checkin_err=False, facts_err=False):
        usage = {"this_agent": {"last_7_days": {"calls": sum(n for _t, n in tools),
                                                "tools": [{"tool": t, "calls": n} for t, n in tools]}}}

        def mcp(key, name, args, timeout=60, path=None):
            self.assertEqual(name, "checkin")
            if checkin_err:
                raise OSError("down")
            return False, {"usage": usage}

        def facts_fn(agent, key, timeout=None):
            if facts_err:
                raise OSError("down")
            return list(facts)
        with mock.patch.object(ds, "mcp_tool", mcp), mock.patch.object(ds, "load_facts", facts_fn):
            return ds.habit_check(AGENT, "ldgr_ag_test")

    def column(self, habits=None):
        checks = [(iid, what, True, step, who) for iid, what, step, who in (("A1", "x", "", ""),)]
        return ds.setup_row_cells(AGENT, self.folder, checks=checks, habits=habits)["full-DDS missing"]

    def test_nothing_used_names_all_four(self):
        h = self.check([("ledger_rows_get", 40), ("ledger_agent_heartbeat", 9)])
        self.assertEqual(h["unused"], ["C12", "C13", "C15", "C16"])
        self.assertEqual(self.column(h), "C12 claims unused, C13 hand-offs unused, C15 check-backs unused, C16 remember unused")

    def test_reads_are_not_uses(self):
        h = self.check([("ledger_agent_job list", 30), ("ledger_agent_handoff list", 4), ("ledger_agent_checkback list", 2),
                        ("ledger_timer_set", 1)])  # the plugin's own one 6 AM timer
        self.assertEqual(h["unused"], ["C12", "C13", "C15", "C16"])

    def test_everything_used_reads_none(self):
        h = self.check([("ledger_agent_job", 3), ("ledger_agent_handoff", 1), ("ledger_agent_checkback", 1)],
                       facts=[("deploys are XPM's", "Web-App", "2026-11-28")])
        self.assertEqual((h["unused"], h["unknown"]), ([], []))
        self.assertEqual(self.column(h), "none")

    def test_timers_count_as_check_backs_past_the_plugins_own(self):
        h = self.check([("ledger_timer_set", 2)], facts=[("x", "other-agent", "2026-11-28")])
        self.assertEqual(h["unused"], ["C12", "C13", "C16"])  # another agent's fact is not this one's

    def test_not_known_is_said_never_none(self):
        h = self.check([], checkin_err=True, facts_err=True)
        self.assertEqual(h["unknown"], ["C12", "C13", "C15", "C16"])
        col = self.column(h)
        self.assertNotEqual(col, "none")
        self.assertIn("C12 claims use not known", col)
        ds._drop(ds._habits_path(AGENT))
        self.assertIn("C16 remember use not known", self.column())  # never checked yet: not known

    def test_the_saved_check_is_what_who_are_you_rows_show(self):
        self.check([("ledger_agent_job", 1)], facts=[("f", "web-app", "2026-11-28")])
        self.assertEqual(self.column(), "C13 hand-offs unused, C15 check-backs unused")

    def test_setup_items_come_first(self):
        h = self.check([("ledger_agent_job", 1), ("ledger_agent_handoff", 1), ("ledger_agent_checkback", 1)])
        checks = [("A3", "The computer is unlocked.", False, "type **datastash sync**", "the person")]
        col = ds.setup_row_cells(AGENT, self.folder, checks=checks, habits=h)["full-DDS missing"]
        self.assertEqual(col, "A3 The computer is unlocked, C16 remember unused")


if __name__ == "__main__":
    unittest.main()
