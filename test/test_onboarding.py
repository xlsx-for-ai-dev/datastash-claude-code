"""Offline tests for plugin 1.0.18: the install onboarding (owner 11-25, verbatim), one step at a time.
- Every install ends with the sync line while sync is off; with sync on, the "Synced" line and the status ask.
- "datastash sync" typed by the person (checked in the transcript) turns keeping on and opens ONE connector sync start;
  the model can never turn it on (no typed phrase: refused). The old long keep phrase still works.
- After sync: held up to the pad while the hours counter runs, then the "Synced" line, relayed word for word.
- owner 11-25 (opt-out, not an ask): "datastash sync" also opens one start_chatstash and turns timers on. After the
  status: the feature list (one table, bold names), the sessions line, the hours line (only from the counter's totals,
  never made up) and how to see the list again, last. No transcripts: no hours line. A counter still running: the line
  comes at the next turn. "do not save my sessions" / "save my sessions"; "turn off timers" / "turn on timers".
- "datastash functions" shows the list again. The clean-sessions guard lets the agent's own skills run.
No network; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_onboarding.py"""
import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_install import Base, LIST, TOOL, ds  # noqa: E402
import harness  # noqa: E402


class OnboardBase(Base):
    def setUp(self):
        super().setUp()
        self.keep = {k: getattr(ds, k) for k in ("CONSENT_PATH", "HOURS_PATH", "HOURS_RUN_PATH", "ONBOARD_PAD")}
        ds.CONSENT_PATH = os.path.join(ds.CONF, "status-consent.json")
        ds.HOURS_PATH = os.path.join(ds.CONF, "hours-estimate.json")
        ds.HOURS_RUN_PATH = os.path.join(ds.STATE, "hours-run.json")
        ds.ONBOARD_PAD = 0.6
        os.makedirs(ds.STATE, exist_ok=True)

    def tearDown(self):
        for k, v in self.keep.items():
            setattr(ds, k, v)
        super().tearDown()

    def run_checks(self, start=0):
        for argv, _cwd in list(self.spawned[start:]):
            if len(argv) > 3 and argv[2] == "status-verify":
                ds.cmd_status_verify([argv[3]])

    def typed_phrase(self, text, sid="s1"):
        start = len(self.spawned)
        out, _pid = self.prompt(text, sid=sid)
        self.run_checks(start)
        return out

    def install(self):
        self.installed()
        self.slash("/datastash:install")
        out = self.tool("PostToolUse", "ledger_agents_list", {}, dict(LIST, agents=LIST["agents"][:1]))
        text = out["hookSpecificOutput"]["updatedMCPToolOutput"][0]["text"]  # what the model relays (markdown)
        self.assertEqual(out["systemMessage"], ds._plain_typed(text))  # the plain line: no "**" there
        self.assertNotIn("**", out["systemMessage"])
        return text

    def hours(self, h=33.04, **more):
        ds.write_private(ds.HOURS_PATH, json.dumps(dict({"hours": h, "plans": 1, "agents": 2, "subagents": 9}, **more)))

    def sync_call(self, event, action="start", answer=None):
        return self.tool(event, "sync", {"action": action}, answer)


class SyncOff(OnboardBase):
    def test_install_ends_with_the_sync_line_and_starts_the_counter(self):
        text = self.install()
        self.assertTrue(text.endswith("\nType **datastash sync** to keep your agents' status, what you tell them to "
                                      "remember, and your data as you go."))
        self.assertNotIn("Synced.", text)
        self.assertEqual(ds.onboard_state("s1")["stage"], "sync")
        self.assertTrue([a for a, _ in self.spawned if a[-2:] == ["--out", ds.HOURS_PATH] and a[-3] == ds.HOURS_SCRIPT])

    def test_the_model_cannot_turn_sync_on(self):
        self.install()
        out = self.sync_call("PreToolUse")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(out["systemMessage"], ds.SYNC_NOT_TYPED_LINE)
        self.assertIsNone(self.sync_call("PostToolUse", answer={"sync": "on"}))  # no grant: nothing recorded
        self.assertFalse(ds.sync_on())
        self.assertNotEqual(ds.keep_state(), "yes")
        # words the model typed (not the person's typed entry) prove nothing
        self.prompt("datastash sync", source="sdk")
        self.run_checks()
        self.assertEqual(self.sync_call("PreToolUse")["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_typed_datastash_sync_turns_on_keep_and_one_sync_start(self):
        self.install()
        out = self.typed_phrase("Datastash sync.")
        self.assertIn('action "start"', out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(ds.keep_state(), "yes")
        self.assertIsNone(self.sync_call("PreToolUse"))  # the call goes out
        self.hours()
        t0 = time.time()
        post = self.sync_call("PostToolUse", answer={"sync": "on", "say_to_user": "Sync is on: ..."})
        self.assertLess(time.time() - t0, 0.5)  # the counter is done: no wait
        ai = post["hookSpecificOutput"]["additionalContext"]
        self.assertIn("----\n" + ds.SYNCED_LINE + "\n----", ai)
        self.assertEqual(ds.SYNCED_LINE, "Synced. Want to see what all your agents are doing right now? Say "
                                         "**status of all my agents**.")
        self.assertTrue(ds.sync_on())
        self.assertEqual(ds.onboard_state("s1")["stage"], "status")
        # used once: a second start is the model's own
        self.assertEqual(self.sync_call("PreToolUse")["hookSpecificOutput"]["permissionDecision"], "deny")
        # owner 11-25: datastash sync also saves the person's sessions: the one start_chatstash call, then the Synced
        # reply stands (no second relay)
        self.assertIn('"start_chatstash"', ai.split("----")[0])
        self.assertIsNone(self.sync_call("PreToolUse", "start_chatstash"))
        self.assertIsNone(self.sync_call("PostToolUse", "start_chatstash", {"chatstash": "on"}))
        self.assertTrue(ds.sessions_on())
        self.assertEqual(self.sync_call("PreToolUse", "start_chatstash")["hookSpecificOutput"]["permissionDecision"],
                         "deny")
        # and timers (owner 11-25): the AI's rule says so; the server is not told until it says it takes timers
        self.assertTrue(ds.timers_choice()["on"])
        self.assertEqual(ds.timers_rule(), ds.TIMERS_ON_RULE)
        self.assertFalse(ds.server_takes_timers())

    def test_the_real_sync_answer_two_text_blocks(self):
        """Real e2e 11-25: the connector answers with its JSON block and then a plain-text line."""
        self.install()
        self.typed_phrase("datastash sync")
        self.assertIsNone(self.sync_call("PreToolUse"))
        resp = [{"type": "text", "text": json.dumps({"sync": "on", "chatstash": "off", "say_to_user": "Sync is on."})},
                {"type": "text", "text": "This person has datastash settings on (their own); checkin lists them."}]
        post = self.hook(ds.cmd_agent_tool, {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": TOOL + "sync",
                                             "tool_input": {"action": "start"}, "cwd": self.folder, "tool_response": resp})
        self.assertIn("----\n" + ds.SYNCED_LINE + "\n----", post["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(ds.sync_on())

    def test_a_queued_prompt_under_another_id_still_counts_once(self):
        """Real e2e 11-25: typed while Claude Code was busy, the entry is "queued" under another prompt id."""
        self.install()
        start = len(self.spawned)
        self.prompt("datastash sync", entry="datastash sync", source="queued")
        # rewrite the entry's prompt id, as Claude Code did
        tp = self.transcript("s1")
        rows = [json.loads(x) for x in open(tp)]
        rows[-1]["promptId"] = "00000000-0000-4000-8000-000000000001"
        rows[-1]["origin"] = {"kind": "human"}
        with open(tp, "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))
        self.run_checks(start)
        self.assertEqual(ds.keep_state(), "yes")
        self.assertIsNone(self.sync_call("PreToolUse"))
        # a typed (not queued) entry under another id never counts
        ds.grant_use("sync", "s1")
        ds.grant_use("chats", "s1")  # datastash sync opened one start_chatstash too
        start = len(self.spawned)
        self.prompt("save my sessions", entry="save my sessions")
        rows = [json.loads(x) for x in open(tp)]
        rows[-1]["promptId"] = "00000000-0000-4000-8000-000000000002"
        with open(tp, "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))
        old = ds.VERIFY_WAIT
        ds.VERIFY_WAIT = 0.5
        try:
            self.run_checks(start)
        finally:
            ds.VERIFY_WAIT = old
        self.assertFalse(ds.grant_ok("chats", "s1"))

    def test_sync_datastash_word_order_too(self):
        self.assertEqual(ds.onboard_phrase("sync datastash"), "sync")
        self.assertEqual(ds.onboard_phrase("please datastash sync my stuff"), None)

    def test_the_old_keep_phrase_still_works(self):
        self.install()
        self.typed_phrase(ds.KEEP_PHRASE)
        self.assertEqual(ds.keep_state(), "yes")
        self.assertFalse(ds.sync_on())  # keep only: the connector's sync waits for "datastash sync"

    def test_the_hold_waits_for_a_running_counter_but_never_past_the_pad(self):
        self.install()
        self.typed_phrase("datastash sync")
        ds.write_private(ds.HOURS_RUN_PATH, json.dumps({"at": time.time() + 5}))  # running, no result yet
        t0 = time.time()
        self.sync_call("PostToolUse", answer={"sync": "on"})
        self.assertGreaterEqual(time.time() - t0, 0.55)
        self.assertLess(time.time() - t0, 2.0)


class SyncOn(OnboardBase):
    def turn_on(self):
        ds.record_keep_all(True, "typed")
        ds.record_sync_on()

    def test_install_with_sync_on_says_synced_and_asks_status(self):
        self.turn_on()
        text = self.install()
        self.assertTrue(text.endswith("\n" + ds.SYNCED_LINE))
        self.assertNotIn("datastash sync**", text)
        self.assertEqual(ds.onboard_state("s1")["stage"], "status")

    def test_status_then_list_sessions_line_then_hours_last(self):
        self.turn_on()
        ds.record_sessions(True)
        self.install()
        self.hours(33.04)
        out, _ = self.prompt("status of all my agents")
        ai = out["hookSpecificOutput"]["additionalContext"]
        want = (ds.features_post() + "\n\nWe turned on saving your sessions. Type **do not save my sessions** if you "
                "don't want an easy and fast way to see what you've done before.\n\nLast week, we estimate you would "
                "have gotten **33** more hours of coding done using datastash. This week will be great!\n\nEverything "
                "in that list is already turned on for you. To see it again any time, type **datastash functions**.")
        self.assertIn(want, ai)
        self.assertTrue(ai.rstrip().endswith("**datastash functions**.\n----"))  # the last prompt
        self.assertNotIn("save my chats", ai)  # no ask any more
        self.assertNotIn("standing yes", ai)  # owner 11-25: no house rules at the end
        self.assertEqual(ds.onboard_state("s1")["stage"], "done")
        # a second status is a plain status
        out2, _ = self.prompt("status of all my agents")
        self.assertNotIn("Seeing your agents", json.dumps(out2 or {}))

    def test_sessions_off_no_sessions_line(self):
        self.turn_on()
        ds.record_sessions(False)
        self.install()
        out, _ = self.prompt("status of all my agents")
        self.assertNotIn("We turned on saving", out["hookSpecificOutput"]["additionalContext"])

    def test_do_not_save_my_sessions_and_save_my_sessions(self):
        ds.record_sessions(True)
        for phrase in ("do not save my sessions", "Don't save my sessions."):
            out = self.typed_phrase(phrase)
            ai = out["hookSpecificOutput"]["additionalContext"]
            self.assertIn('"stop_chatstash"', ai)
            self.assertIn(ds.SESSIONS_OFF_LINE, ai)
            self.assertFalse(ds.sessions_on())
        self.assertEqual(ds.SESSIONS_OFF_LINE, "Session saving is off. To turn it back on, type **save my sessions**.")
        self.typed_phrase("save my sessions")
        self.assertIsNone(self.sync_call("PreToolUse", "start_chatstash"))
        post = self.sync_call("PostToolUse", "start_chatstash", {"chatstash": "on"})
        self.assertIn("----\n" + ds.SESSIONS_ON_LINE + "\n----", post["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(ds.sessions_on())

    def test_the_shown_status_goes_through_the_model_so_the_bold_renders(self):
        ds.onboard_set("s1", "status")
        inp = {"session_id": "s1", "prompt": "status of all my agents"}
        got = ds.onboard_after_status(inp, {"show": "Your agents:\n- api: working"})
        self.assertIsInstance(got, str)
        self.assertIn("----\nYour agents:\n- api: working\n\n" + ds.features_post(), got)

    def test_save_my_sessions_untyped_is_refused(self):
        out = self.sync_call("PreToolUse", "start_chatstash")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(out["systemMessage"], ds.CHATS_NOT_TYPED_LINE)

    def test_a_second_install_runs_the_same_flow(self):
        self.turn_on()
        self.install()
        self.prompt("status of all my agents")
        self.assertEqual(ds.onboard_state("s1")["stage"], "done")
        text = self.install()
        self.assertTrue(text.endswith("\n" + ds.SYNCED_LINE))
        self.assertEqual(ds.onboard_state("s1")["stage"], "status")
        out, _ = self.prompt("status of all my agents")
        self.assertIn(ds.features_post(), out["hookSpecificOutput"]["additionalContext"])


class Hours(OnboardBase):
    def test_no_transcripts_no_line(self):
        ds.write_private(ds.HOURS_PATH, json.dumps({"none": True}))
        self.assertIsNone(ds.hours_text())
        self.assertEqual(ds.hours_status(), "none")

    def test_no_result_file_no_line(self):
        self.assertIsNone(ds.hours_text())

    def test_small_or_bad_numbers_no_line(self):
        for h in (0.2, -1, "33", True, float("nan")):
            self.hours(h)
            self.assertIsNone(ds.hours_text(), h)

    def test_formats(self):
        for h, x in ((33.04, "33"), (2.54, "2.5"), (3.0, "3"), (9.96, "10")):
            self.hours(h)
            self.assertIn(f"**{x}** more hours", ds.hours_text())

    def test_a_running_counter_shows_the_line_at_the_next_turn(self):
        ds.record_keep_all(True, "typed")
        ds.record_sync_on()
        self.install()
        ds.write_private(ds.HOURS_RUN_PATH, json.dumps({"at": time.time()}))  # running past the pad
        out, _ = self.prompt("status of all my agents")
        ai = out["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("Last week", ai)
        self.assertNotIn("datastash functions", ai)
        self.assertTrue(ds.onboard_state("s1")["hours_pending"])
        self.hours(12.0)  # the counter finishes
        out, _ = self.prompt("thanks")
        self.assertEqual(out["systemMessage"].split("\n")[:2],
                         ["Last week, we estimate you would have gotten 12 more hours of coding done using datastash. "
                          "This week will be great!",
                          'Everything in that list is already turned on for you. To see it again any time, type '
                          '"datastash functions".'])
        out, _ = self.prompt("thanks again")
        self.assertNotIn("Last week", json.dumps(out or {}))  # once

    def test_kick_skips_a_recent_result_and_a_running_count(self):
        self.hours()
        ds.hours_kick()
        self.assertFalse(self.spawned)
        os.utime(ds.HOURS_PATH, (time.time() - 7200, time.time() - 7200))
        ds.write_private(ds.HOURS_RUN_PATH, json.dumps({"at": time.time()}))
        ds.hours_kick()
        self.assertFalse(self.spawned)


class Timers(OnboardBase):
    def test_never_chosen_no_rule(self):
        self.assertEqual(ds.timers_rule(), "")

    def test_turn_off_then_turn_on_timers(self):
        ds.set_timers(True, "sync")
        out = self.typed_phrase("turn off timers")
        ai = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn(ds.TIMERS_OFF_LINE, ai)
        self.assertNotIn("timers_off", ai)  # the server does not take timers yet: nothing new is sent
        self.assertEqual(ds.timers_rule(), ds.TIMERS_OFF_RULE)
        out = self.typed_phrase("Turn on timers.")
        self.assertIn(ds.TIMERS_ON_LINE, out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(ds.timers_rule(), ds.TIMERS_ON_RULE)
        self.assertIn("**turn off timers**", ds.TIMERS_ON_LINE)

    def test_turn_on_timers_untyped_does_nothing(self):
        ds.set_timers(False, "typed")
        self.prompt("turn on timers", source="sdk")
        self.run_checks()
        self.assertEqual(ds.timers_rule(), ds.TIMERS_OFF_RULE)
        out = self.sync_call("PreToolUse", "timers_on")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(out["systemMessage"], ds.TIMERS_NOT_TYPED_LINE)

    def test_server_that_says_timers_gets_the_calls(self):
        ds.set_timers(True, "sync")
        ds.timers_server_said({"timers": "on"})
        self.assertTrue(ds.server_takes_timers())
        out = self.typed_phrase("turn off timers")
        self.assertIn('action "timers_off"', out["hookSpecificOutput"]["additionalContext"])
        out = self.typed_phrase("turn on timers")
        self.assertIn('action "timers_on"', out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.sync_call("PreToolUse", "timers_on"))
        self.assertIsNone(self.sync_call("PostToolUse", "timers_on", {"timers": "on"}))
        self.assertEqual(self.sync_call("PreToolUse", "timers_on")["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_an_answer_without_the_field_never_opens_it(self):
        ds.set_timers(True, "sync")
        for body in ({}, {"timers": "yes"}, {"timers": True}, None):
            ds.timers_server_said(body)
        self.assertFalse(ds.server_takes_timers())


class Functions(OnboardBase):
    def test_datastash_functions_shows_the_list(self):
        for phrase in ("datastash functions", "Functions datastash"):
            out, _ = self.prompt(phrase)
            self.assertIn("----\n" + ds.features_post() + "\n----", out["hookSpecificOutput"]["additionalContext"])

    def test_the_list_is_one_table_with_bold_names(self):
        post = ds.features_post()
        self.assertTrue(post.startswith("Seeing your agents\n  - **Datastash main:** one main session that sees and "
                                        "answers all your agents.\n  - **Status of all my agents:** every agent's "
                                        "progress in half a second."))
        self.assertIn("  - **Time per card:** time and tokens for each piece of work.", post)
        self.assertIn('  - **Lists:** "keep a list of…", for leads, contacts, anything.', post)
        n = sum(len(items) for _h, items in ds.FEATURES)
        self.assertEqual(post.count("  - **"), n)


class CleanOwnSkills(OnboardBase):
    def setUp(self):
        super().setUp()
        self.home = harness.tmpdir(self, prefix="skills-", dir=harness.ROOT)
        self.old_home = ds.HOME
        ds.HOME = self.home
        os.makedirs(os.path.join(self.home, ".claude", "skills", "rehydrate"))
        open(os.path.join(self.home, ".claude", "skills", "rehydrate", "SKILL.md"), "w").close()

    def tearDown(self):
        ds.HOME = self.old_home
        super().tearDown()

    def test_own_skill_runs_others_stay_refused(self):
        self.assertTrue(ds.clean_tool_ok({"tool_name": "Skill", "tool_input": {"skill": "rehydrate"}}))
        self.assertFalse(ds.clean_tool_ok({"tool_name": "Skill", "tool_input": {"skill": "someplugin:rehydrate"}}))
        self.assertFalse(ds.clean_tool_ok({"tool_name": "Skill", "tool_input": {"skill": "unknown"}}))
        self.assertFalse(ds.clean_tool_ok({"tool_name": "Skill", "tool_input": {"skill": "../skills/rehydrate"}}))

    def test_a_project_skill_counts_too(self):
        os.makedirs(os.path.join(self.folder, ".claude", "skills", "proj"))
        open(os.path.join(self.folder, ".claude", "skills", "proj", "SKILL.md"), "w").close()
        self.assertTrue(ds.clean_tool_ok({"tool_name": "Skill", "tool_input": {"skill": "proj"}, "cwd": self.folder}))
        self.assertFalse(ds.clean_tool_ok({"tool_name": "Skill", "tool_input": {"skill": "proj"}}))


if __name__ == "__main__":
    unittest.main()
