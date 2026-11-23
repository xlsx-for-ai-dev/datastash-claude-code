#!/usr/bin/env python3
"""Offline tests for XLI-308 (plugin 0.6.0): datastash spots repeated outside lookups and keeps them ready.
- The PostToolUse Bash hook counts repeated outside lookups per agent, in plain code (gh pr/run/api pulls|actions,
  linear, jira, sentry-cli), storing only a normalized command shape: never output, arguments or secrets.
- Past 10 of one service in a day, status shows one numbered ask, once.
- "do K", typed by the person (the checker proves it): GitHub, Linear and Jira call ledger_feeds_setup with the agent's
  key; the plugin itself picks up the hook secret with this machine's verifier (the AI never sees a code). Other tools:
  recorded as declined for now, nothing sent.
- From then on the hook adds one line when the agent runs that lookup again: "datastash has this: ...".
- The person's status shows the server's "Changed in the last hour" block.
No network, no key; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_lookups.py"""
import json
import os
import stat
import subprocess
import sys
import time
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import Base, ds, WB  # noqa: E402  (the same harness, plugin copy and fixtures)

HOOK_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def bash(cmd, event="PostToolUse", sid="sess1", cwd=None):
    return {"session_id": sid, "hook_event_name": event, "tool_name": "Bash", "tool_input": {"command": cmd},
            "tool_response": {"stdout": "PR #99 secret-output ghp_" + "z" * 36}, "cwd": cwd}


class Shape(unittest.TestCase):
    def test_lookups_are_classified_to_a_plain_shape(self):
        cases = {
            "gh pr view 123 --repo acme/web": ("github", "gh pr view"),
            "gh pr checks 5": ("github", "gh pr checks"),
            "gh pr list --state open": ("github", "gh pr list"),
            "gh run list -L 5": ("github", "gh run list"),
            "gh run view 991 --log": ("github", "gh run view"),
            "gh api repos/acme/web/pulls/5": ("github", "gh api pulls"),
            "gh api -X GET repos/acme/web/actions/runs?branch=main": ("github", "gh api actions"),
            "linear issue view XLI-1": ("linear", "linear issue"),
            "jira issue list -q 'project = X'": ("jira", "jira issue"),
            "sentry-cli issues list": ("sentry", "sentry-cli issues"),
            "cd /tmp && gh pr list": ("github", "gh pr list"),
            "GH_TOKEN=ghp_abcdefabcdefabcdefabcdefabcdefabcdef gh pr view 1": ("github", "gh pr view"),
            "~/bin/linear-card get XLI-308": None,
        }
        for cmd, want in cases.items():
            self.assertEqual(ds.lookup_shape(cmd), want, cmd)

    def test_other_commands_are_not_lookups(self):
        for cmd in ("git status", "echo gh pr view", "gh api user", "gh auth status", "ls gh", "", "gh",
                    "gh pr create --title x", "gh pr merge 5", "jira", "python3 -c 'gh pr view'"):
            self.assertIsNone(ds.lookup_shape(cmd), cmd)
        self.assertIsNone(ds.lookup_shape(None))

    def test_no_shape_keeps_a_target_name(self):
        # XLI-323: the word after linear, jira or sentry-cli is kept only when it is a known subcommand; a team,
        # project, org, host or any other name in that place is never kept (allow only what is known safe).
        for cmd in ("linear acme-team", "linear ops issue list", "jira myproj", "jira acme-corp issue list",
                    "sentry-cli acme-org", "sentry-cli my-host issues", "linear person-name", "jira bucket"):
            got = ds.lookup_shape(cmd)
            if got is not None:
                self.assertEqual(len(got[1].split()), 2, cmd)
                self.assertNotIn(got[1].split()[1], ("acme-team", "ops", "myproj", "acme-corp", "acme-org",
                                                     "my-host", "person-name", "bucket"), cmd)
        # Names after a known subcommand (an org, a project, a host, a card id) are never kept.
        cases = {
            "linear team list": ("linear", "linear team"),
            "linear issue view XLI-1 --team acme": ("linear", "linear issue"),
            "jira project list": ("jira", "jira project"),
            "jira issue view PROJ-7 --server https://x.example": ("jira", "jira issue"),
            "sentry-cli issues list --org acme": ("sentry", "sentry-cli issues"),
            "sentry-cli releases list -p web": ("sentry", "sentry-cli releases"),
            "gh pr view 123 --repo acme/web": ("github", "gh pr view"),
        }
        for cmd, want in cases.items():
            self.assertEqual(ds.lookup_shape(cmd), want, cmd)
        # Every word of every kept shape comes from the plugin's own fixed list, never from the command.
        known = {w for v in ds.LOOKUP_SUBCOMMANDS.values() for w in v} | set(ds.LOOKUP_TOOLS) | {"api", "pulls",
                                                                                                 "actions"}
        known |= set(ds._GH_LOOKUPS) | {w for v in ds._GH_LOOKUPS.values() for w in v}
        for cmd in ("linear acme-team", "jira myproj", "sentry-cli acme-org", "linear issue", "gh run watch 5"):
            got = ds.lookup_shape(cmd)
            if got is not None:
                self.assertTrue(set(got[1].split()) <= known, (cmd, got))


class LookupBase(Base):
    @staticmethod
    def move_counts_aside():
        for p in (ds.LOOKUPS_PATH, ds.LOOKUP_OFFERS_PATH):
            if os.path.exists(p):
                os.replace(p, os.path.join(ds.CONF, f"old-{os.path.basename(p)}-{time.time()}"))

    def setUp(self):
        super().setUp()
        olds = [ds.LOOKUPS_PATH, ds.LOOKUP_OFFERS_PATH] + [os.path.join(ds.CONF, n) for n in os.listdir(ds.CONF)
                                                            if n.startswith("hook-") and n.endswith(".secret")]
        if os.path.isdir(ds.PENDING):
            olds += [os.path.join(ds.PENDING, n) for n in os.listdir(ds.PENDING) if n.startswith("hook-")]
        for p in olds:
            if os.path.exists(p):
                os.replace(p, os.path.join(ds.CONF, f"old-{os.path.basename(p)}-{time.time()}"))
        self.addCleanup(self.move_counts_aside)  # never leave counts for a later test module in the same process
        base = ds.http
        self.feed_answer = None

        def http(method, path, key=None, body=None, form=None, timeout=40, accept="application/json"):
            if path == ds.MCP_PATH:
                self.calls.append((method, path, key, body))
                args = body["params"]["arguments"]
                svc = args["services"][0]
                feed = self.feed_answer(svc, args) if self.feed_answer else self.default_feed(svc, args)
                text = json.dumps({"workbook_id": WB, "feeds": [feed], "read": "Answer from the tabs."})
                return 200, "application/json", json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
                    "content": [{"type": "text", "text": text}]}})
            if path == ds.HOOK_PICKUP_PATH:
                self.calls.append((method, path, key, form))
                if form.get("secret"):
                    return 200, "text/plain", "ok: the hook signs with that secret now\n"
                return 200, "text/plain", "dshs_" + "s" * 43 + "\n"
            return base(method, path, key, body, form, timeout)
        ds.http = http

    def default_feed(self, svc, args):
        tab = {"github": "PRs and CI", "linear": "Cards", "jira": "Issues"}[svc]
        ex = {"github": "owner/repo #123", "linear": "XLI-12", "jira": "PROJ-7"}[svc]
        out = {"service": svc, "ok": True, "tab": tab, "key_column": "key", "example_key": ex, "hook_id": HOOK_ID,
               "url": f"https://getdatastash.com/api/v1/in/{HOOK_ID}", "reused": False, "pickup_code": "dshp_" + "c" * 30,
               "bound": True, "steps": [f"Step one for {svc}.", "Step two."]}
        if svc == "github":
            out["register"] = [{"repo": r, "line": f"gh api repos/{r}/hooks --method POST --input -"} for r in args.get("repos") or []]
        return out

    def lookups(self, cmd, n, sid="sess1"):
        for _ in range(n):
            self.hook(ds.cmd_agent_tool, bash(cmd, sid=sid, cwd=self.folder))

    def status_text(self, for_person=True):
        return "\n".join(ds.render_status_all(self.folder, ds.this_session(), for_person=for_person))

    def offer_number(self):
        view = ds.read_view("sess1")
        return next(int(k) for k, v in view["numbers"].items() if v.get("lookup"))


class Counting(LookupBase):
    def test_post_tool_use_bash_counts_per_agent_and_stores_only_the_shape(self):
        self.lookups("GH_TOKEN=ghp_" + "q" * 36 + " gh pr view 99 --repo secret-org/hidden-repo", 3)
        self.lookups("linear issue view XLI-77", 1)
        with open(ds.LOOKUPS_PATH) as f:
            rec = json.load(f)
        self.assertEqual(rec["day"], time.strftime("%Y-%m-%d", time.localtime()))
        self.assertEqual(rec["counts"], {"web-app": {"gh pr view": 3, "linear issue": 1}})
        with open(ds.LOOKUPS_PATH) as f:
            text = f.read()
        for bad in ("99", "secret", "hidden", "ghp_", "XLI-77", "output"):
            self.assertNotIn(bad, text)
        self.assertEqual(stat.S_IMODE(os.stat(ds.LOOKUPS_PATH).st_mode), 0o600)

    def test_pre_tool_use_other_tools_and_non_lookups_do_not_count(self):
        self.hook(ds.cmd_agent_tool, bash("gh pr view 1", event="PreToolUse", cwd=self.folder))
        self.hook(ds.cmd_agent_tool, bash("gh pr view 1", event="PostToolUseFailure", cwd=self.folder))
        self.hook(ds.cmd_agent_tool, {"session_id": "sess1", "hook_event_name": "PostToolUse", "tool_name": "Read",
                                      "tool_input": {"file_path": "gh pr view"}, "cwd": self.folder})
        self.hook(ds.cmd_agent_tool, bash("git log", cwd=self.folder))
        self.assertEqual(ds.read_json(ds.LOOKUPS_PATH, {}).get("counts", {}), {})

    def test_a_folder_with_no_agent_counts_under_a_folder_tag_without_the_path(self):
        other = os.path.realpath(os.path.join(ds.CONF, "..", "unlinked-proj"))
        os.makedirs(other, exist_ok=True)
        os.environ["CLAUDE_PROJECT_DIR"] = other
        self.hook(ds.cmd_agent_tool, bash("gh run list", cwd=other))
        counts = ds.read_json(ds.LOOKUPS_PATH, {})["counts"]
        (who,) = counts
        self.assertTrue(who.startswith("folder:"))
        self.assertNotIn("unlinked", who)

    def test_yesterdays_counts_start_over(self):
        ds.write_private(ds.LOOKUPS_PATH, json.dumps({"day": "2001-01-01", "counts": {"web-app": {"gh pr view": 50}}}))
        self.lookups("gh pr view 1", 1)
        self.assertEqual(ds.read_json(ds.LOOKUPS_PATH, {})["counts"], {"web-app": {"gh pr view": 1}})

    def test_no_counting_in_the_persons_main_session(self):
        ds.write_private(ds._main_path("main1"), "{}")
        self.hook(ds.cmd_agent_tool, bash("gh pr view 1", sid="main1", cwd=self.folder))
        self.assertEqual(ds.read_json(ds.LOOKUPS_PATH, {}).get("counts", {}), {})

    def test_the_hook_prints_nothing_for_a_lookup_nobody_keeps_yet(self):
        ds.hook_input = lambda: bash("gh pr view 1", cwd=self.folder)
        rc, out, err = self.run_cmd(ds.cmd_agent_tool, [])
        self.assertEqual((rc, out), (0, ""))


class Offer(LookupBase):
    def setUp(self):
        super().setUp()
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        ds.write_private(ds.INSTALL_ASKED_PATH, "{}")

    def test_ten_is_not_enough_eleven_shows_one_numbered_ask_once(self):
        self.lookups("gh pr view 1", 6)
        self.lookups("gh pr checks 1", 4)
        self.assertNotIn("Want datastash to keep that ready?", self.status_text())
        self.lookups("gh api repos/a/b/pulls", 1)
        text = self.status_text()
        k = self.offer_number()
        self.assertIn(f"Your agents asked where PRs stand 11 times today. Want datastash to keep that ready? (do {k})", text)
        self.assertEqual(text.count("Want datastash to keep"), 1)
        numbers = ds.read_view("sess1")["numbers"]
        self.assertEqual(k, max(int(x) for x in numbers))
        self.assertEqual(numbers[str(k)], {"lookup": "github", "agent": "web-app"})
        self.lookups("gh pr view 1", 30)
        self.assertNotIn("Want datastash to keep", self.status_text())  # once
        self.assertNotIn("Want datastash to keep", self.status_text(for_person=False))

    def test_the_ai_facing_status_shows_it_outside_the_agents_frame(self):
        self.lookups("linear issue view X-1", 11)
        lines = ds.render_status_all(self.folder, ds.this_session())
        end = next(i for i, x in enumerate(lines) if x.startswith("<<end datastash-data "))
        hit = next(i for i, x in enumerate(lines) if "Want datastash to keep that ready?" in x)
        self.assertGreater(hit, end)
        self.assertIn("asked where Linear cards stand 11 times today", lines[hit])

    def test_counts_add_up_across_agents_and_the_busiest_agent_is_named(self):
        ds.write_private(ds.conf_path("api"), json.dumps({"name": "api", "workbook_id": WB, "projects": ["/nowhere"]}))
        ds.write_private(ds.LOOKUPS_PATH, json.dumps({"day": time.strftime("%Y-%m-%d", time.localtime()), "counts": {
            "web-app": {"jira issue": 4}, "api": {"jira issue": 7}}}))
        self.status_text()
        k = self.offer_number()
        self.assertEqual(ds.read_view("sess1")["numbers"][str(k)], {"lookup": "jira", "agent": "api"})


class DoK(LookupBase):
    def setUp(self):
        super().setUp()
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        ds.write_private(ds.INSTALL_ASKED_PATH, "{}")

    def offer(self, cmd):
        self.lookups(cmd, 11)
        self.prompt("status")
        self.calls.clear()
        return self.offer_number()

    def test_typed_do_k_sets_up_the_github_feed_with_the_agents_key_and_picks_up_the_secret(self):
        subprocess.run(["git", "init", "-q", self.folder], check=True)
        subprocess.run(["git", "-C", self.folder, "remote", "add", "origin", "git@github.com:acme/web-app.git"], check=True)
        k = self.offer("gh pr view 1")
        ctx = self.prompt(f"do {k}")
        self.assertIsNone(self.last_hook.get("decision"))  # not blocked: the checker needs the typed entry
        self.assertIn("datastash lookups steps github", ctx)
        mcp = [c for c in self.calls if c[1] == ds.MCP_PATH]
        self.assertEqual(len(mcp), 1)
        _m, _p, key, body = mcp[0]
        self.assertEqual(key, "ldgr_ag_test")
        self.assertEqual(body["method"], "tools/call")
        self.assertEqual(body["params"]["name"], "ledger_feeds_setup")
        args = body["params"]["arguments"]
        self.assertEqual((args["workbook_id"], args["services"], args["repos"]), (WB, ["github"], ["acme/web-app"]))
        self.assertRegex(args["code_challenge"], r"^[A-Za-z0-9_\-]{43}$")
        pick = [c for c in self.calls if c[1] == ds.HOOK_PICKUP_PATH]
        self.assertEqual(len(pick), 1)
        self.assertEqual(pick[0][3]["code"], "dshp_" + "c" * 30)
        self.assertEqual(ds.challenge_of(pick[0][3]["verifier"]), args["code_challenge"])
        secret = os.path.join(ds.CONF, f"hook-{HOOK_ID}.secret")
        with open(secret) as f:
            self.assertEqual(f.read().strip(), "dshs_" + "s" * 43)
        self.assertEqual(stat.S_IMODE(os.stat(secret).st_mode), 0o600)
        self.assertEqual(ds.read_json(ds.LOOKUP_OFFERS_PATH, {})["github"]["state"], "set_up")
        rc, out, _ = self.run_cmd(ds.cmd_lookups, ["steps", "github"])
        self.assertEqual(rc, 0)
        self.assertIn("<<datastash-data ", out)
        self.assertIn("gh api repos/acme/web-app/hooks", out)
        self.assertIn("Step one for github.", out)
        for bad in ("dshp_", "dshs_", "verifier"):
            self.assertNotIn(bad, out)
        self.assertTrue(any("GitHub" in x for x in self.outcomes()))

    def test_after_set_up_the_hook_adds_one_line_on_that_lookup(self):
        k = self.offer("gh pr view 1")
        self.prompt(f"do {k}")
        v = self.hook(ds.cmd_agent_tool, bash("gh pr view 12", cwd=self.folder))
        hint = v["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(v["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertTrue(hint.startswith("datastash has this: "), hint)
        self.assertNotIn("\n", hint)
        self.assertIn('"PRs and CI"', hint)
        self.assertIn(urllib.parse.quote("PRs and CI"), hint)
        self.assertIn(WB, hint)
        self.assertIsNone(self.hook(ds.cmd_agent_tool, bash("linear issue view X-1", cwd=self.folder)))  # not kept
        ds.set_clean(commands=False)  # XLI-386: the clean sessions nudge is its own line on PreToolUse
        self.assertIsNone(self.hook(ds.cmd_agent_tool, bash("gh pr view 12", event="PreToolUse", cwd=self.folder)))

    def test_forged_or_headless_do_k_sends_nothing(self):
        k = self.offer("gh pr view 1")
        inp = {"session_id": "sess1", "prompt": f"do {k}", "hook_event_name": "UserPromptSubmit", "cwd": self.folder,
               "prompt_id": "forged-prompt-0001", "transcript_path": self.transcript("sess1")}
        self.forged(inp)
        self.prompt(f"do {k}", writer=self.headless)
        self.assertEqual([c for c in self.calls if c[1] in (ds.MCP_PATH, ds.HOOK_PICKUP_PATH)], [])
        self.assertNotEqual(ds.read_json(ds.LOOKUP_OFFERS_PATH, {}).get("github", {}).get("state"), "set_up")

    def test_do_k_for_a_tool_datastash_does_not_cover_is_declined_for_now(self):
        k = self.offer("sentry-cli issues list")
        text = self.prompt(f"do {k}")
        self.assertEqual(self.last_hook["decision"], "block")
        self.assertIn("doesn't keep Sentry ready yet", text)
        self.assertEqual(ds.read_json(ds.LOOKUP_OFFERS_PATH, {})["sentry"]["state"], "declined-for-now")
        self.assertEqual([c for c in self.calls if c[0] == "POST"], [])
        self.assertIsNone(self.hook(ds.cmd_agent_tool, bash("sentry-cli issues list", cwd=self.folder)))

    def test_linear_waits_for_the_persons_secret_then_the_typed_do_hands_it_in(self):
        k = self.offer("linear issue view X-1")
        self.prompt(f"do {k}")
        self.assertEqual(ds.read_json(ds.LOOKUP_OFFERS_PATH, {})["linear"]["state"], "needs_secret")
        self.assertEqual([c for c in self.calls if c[1] == ds.HOOK_PICKUP_PATH], [])  # no secret yet: nothing sent
        rc, out, _ = self.run_cmd(ds.cmd_lookups, ["steps", "linear"])
        self.assertIn(f"hook-{HOOK_ID}.secret", out)
        self.assertNotIn("set_secret", out.replace("never run set_secret", ""))
        secret = os.path.join(ds.CONF, f"hook-{HOOK_ID}.secret")
        ds.write_private(secret, "lin_wh_" + "k" * 30 + "\n")
        self.prompt("status")
        k2 = self.offer_number()
        self.assertIn(f"(do {k2})", self.status_text())
        self.prompt("status")
        self.calls.clear()
        self.prompt(f"do {self.offer_number()}")
        pick = [c for c in self.calls if c[1] == ds.HOOK_PICKUP_PATH]
        self.assertEqual(len(pick), 1)
        self.assertEqual(pick[0][3]["secret"], "lin_wh_" + "k" * 30)
        self.assertEqual(ds.read_json(ds.LOOKUP_OFFERS_PATH, {})["linear"]["state"], "set_up")

    def test_a_failed_setup_says_so_and_offers_again(self):
        self.feed_answer = lambda svc, args: {"service": svc, "ok": False, "code": "tab_exists", "message": "Tab is there."}
        k = self.offer("gh pr view 1")
        self.prompt(f"do {k}")
        self.assertEqual(ds.read_json(ds.LOOKUP_OFFERS_PATH, {})["github"]["state"], "failed")
        self.assertTrue(any("Tab is there." in x for x in self.outcomes()))
        self.assertIn("Want datastash to keep that ready?", self.status_text())


class Changed(LookupBase):
    """XLI-306: the person's status shows the server's "Changed in the last hour" lines after the statuses."""
    def setUp(self):
        super().setUp()
        self.changed = {"head": "Changed in the last hour", "lines": ["GitHub: acme/web #41 merged", "Linear: XLI-12 Done"],
                        "show": "Show these lines after the statuses."}
        base = ds.http

        def http(method, path, key=None, body=None, form=None, timeout=40, accept="application/json"):
            st, ct, text = base(method, path, key, body, form, timeout)
            if method == "GET" and path.startswith(ds.PERSON_STATUS_PATH + "?") and self.changed is not None:
                b = json.loads(text)
                b["changed"] = self.changed
                text = json.dumps(b)
            return st, ct, text
        ds.http = http

    def test_the_block_follows_the_statuses_for_the_person_and_the_ai(self):
        ds.fetch_status({"slug": ds.PERSON_SLUG, "workbook_id": WB})
        out = ds.render_status_all(self.folder, ds.this_session(), for_person=True)
        i = out.index("Changed in the last hour")
        self.assertEqual(out[i + 1:i + 3], ["GitHub: acme/web #41 merged", "Linear: XLI-12 Done"])
        self.assertGreater(i, next(j for j, x in enumerate(out) if "web-app" in x))
        ai = ds.render_status_all(self.folder, ds.this_session())
        self.assertIn("GitHub: acme/web #41 merged", ai)

    def test_none_when_the_server_sends_none_bad_or_old(self):
        self.changed = None
        ds.fetch_status({"slug": ds.PERSON_SLUG, "workbook_id": WB})
        self.assertNotIn("Changed in the last hour", self.status_text())
        for bad in ({"head": "x", "lines": "text"}, {"lines": [1]}, []):
            self.changed = bad
            ds.fetch_status({"slug": ds.PERSON_SLUG, "workbook_id": WB})
            self.assertNotIn("Changed in the last hour", self.status_text())
        self.changed = {"head": "Changed in the last hour", "lines": ["GitHub: a\x1b[2J b"]}
        ds.fetch_status({"slug": ds.PERSON_SLUG, "workbook_id": WB})
        self.assertIn("GitHub: a [2J b", self.status_text())
        c = ds.read_status_cache(WB)
        ds.save_status_cache(WB, c["agents"], time.time(), merge=False, changed=dict(c["changed"], at=time.time() - 2 * 3600))
        self.assertNotIn("Changed in the last hour", self.status_text())  # over an hour old: never shown as current

    def test_an_agent_key_read_keeps_the_persons_block(self):
        ds.fetch_status({"slug": ds.PERSON_SLUG, "workbook_id": WB})
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})
        self.assertIn("Changed in the last hour", self.status_text())


if __name__ == "__main__":
    unittest.main(verbosity=1)
