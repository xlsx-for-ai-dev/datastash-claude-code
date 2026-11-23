#!/usr/bin/env python3
"""Offline tests for XLI-390 P1 (plugin 1.0.0): /datastash:install and /datastash:uninstall.
- Install: this agent's key only through collect (the connector call carries the S256 challenge of a verifier made
  here; no code or key in any hook output), saved mode 600 (the answer key in the keychain or mode 600), then the
  fixed-format list (name and public or private, no computer) and the removal line.
- Uninstall: the list, then names or "all" typed by the person, signed over agent ids from the list id; model-typed
  text and a note with no command are refused; names that differ only by case or look-alike letters are refused.
- Cleanup: every row of the cleanup table goes, and nothing else; the box goes with its last agent.
- 410 agent_removed: the listener confirms with the dead key and cleans up by itself; a plain 410 does not.
No network; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_install.py"""
import hashlib
import hmac
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import ds  # noqa: E402  (the same harness and plugin copy)
import harness  # noqa: E402

WB = "11111111-2222-3333-4444-555555555555"
OWN, ALPHA, BETA = (str(uuid.UUID(int=i)) for i in (1, 2, 3))
MINT = str(uuid.UUID(int=9))
KEY, ANSWER = "ldgr_ag_" + "k" * 40, "A" * 43
LIST = {"list_id": "list-1", "agents": [{"agent_id": OWN, "name": "web-app", "private": False},
                                        {"agent_id": ALPHA, "name": "alpha‮", "private": True},
                                        {"agent_id": BETA, "name": "beta", "private": False}]}
TOOL = "mcp__claude_ai_datastash__"
PATCHED = ("CONF", "STATE", "PENDING", "OFF", "MAIN_DIR", "http", "_claude_chain", "_kc", "_stop_listener")


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {k: getattr(ds, k) for k in PATCHED}
        root = os.path.realpath(tempfile.mkdtemp(prefix="install-", dir=harness.ROOT))
        ds.CONF, ds.MAIN_DIR = os.path.join(root, "datastash"), os.path.join(root, "datastash main")
        ds.STATE, ds.PENDING, ds.OFF = (os.path.join(ds.CONF, d) for d in ("state", "pending", "off"))
        self.env = os.environ.get("DATASTASH_CONFIG_DIR")
        os.environ["DATASTASH_CONFIG_DIR"] = ds.CONF
        self.folder = os.path.join(root, "web-app")
        os.makedirs(self.folder)
        self.calls, self.answers, self.kc = [], {}, {}
        ds._claude_chain = lambda: [[4242, "start-A"]]
        ds._stop_listener = lambda s: self.calls.append(("stop", s))

        def fake_http(method, path, key=None, body=None, form=None, timeout=40, accept=None):
            self.calls.append((method, path, key, body))
            got = self.answers.get(path, (404, {}))
            if got[0] >= 400:
                raise ds.ApiError(got[0], got[1])
            return got[0], "application/json", json.dumps(got[1])
        ds.http = fake_http
        self.kc_works = False  # the fake OS keychain: off (the mode-600 file) unless a test turns it on

        def fake_kc(op, account, value=None):
            if op == "put" and self.kc_works:
                self.kc[account] = value
            return self.kc_works if op == "put" else self.kc.get(account) if op == "get" else self.kc.pop(account, None)
        ds._kc = fake_kc

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(ds, k, v)
        if self.env is None:
            os.environ.pop("DATASTASH_CONFIG_DIR", None)
        else:
            os.environ["DATASTASH_CONFIG_DIR"] = self.env

    def transcript(self, sid):
        d = os.path.join(ds.CLAUDE_DIR, "projects", "-install")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{sid}.jsonl")

    def typed(self, sid, text, pid, source="typed"):
        v = {"type": "user", "promptId": pid, "message": {"role": "user", "content": text}, "promptSource": source,
             "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
        if source == "typed":
            v["origin"] = {"kind": "human"}
        with open(self.transcript(sid), "a") as f:
            f.write(json.dumps(v) + "\n")

    def hook(self, fn, inp):
        ds.hook_input = lambda: inp
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(fn([]), 0)
        return json.loads(out.getvalue()) if out.getvalue().strip() else None

    def prompt(self, text, sid="s1", source="typed", entry=None):
        pid = str(uuid.uuid4())
        v = self.hook(ds.cmd_prompt, {"session_id": sid, "prompt": text, "hook_event_name": "UserPromptSubmit",
                                      "cwd": self.folder, "prompt_id": pid, "transcript_path": self.transcript(sid)})
        if not (v or {}).get("decision"):
            self.typed(sid, entry or text, pid, source)  # Claude Code writes the entry after the prompt hook
        return v, pid

    def tool(self, event, name, tool_input=None, answer=None, sid="s1"):
        inp = {"session_id": sid, "hook_event_name": event, "tool_name": TOOL + name, "tool_input": tool_input or {},
               "cwd": self.folder}
        if answer is not None:
            inp["tool_response"] = [{"type": "text", "text": json.dumps(answer)}]
        return self.hook(ds.cmd_agent_tool, inp)

    def installed(self):
        ds.save_agent("web-app", OWN, KEY, WB, self.folder)
        c = ds.read_json(ds.conf_path("web-app"), {})
        c["answer_store"] = ds.save_agent_answer("web-app", ANSWER)
        ds.write_private(ds.conf_path("web-app"), json.dumps(c))


def proof_ok(p, kind, targets, pid, typed):
    lines = ["datastash-agents-note/v1", OWN, "list-1", kind, ",".join(sorted(targets)), pid,
             hashlib.sha256(typed.encode()).hexdigest(), str(p["typed_at"]), str(p["iat"]), p["nonce"]]
    return hmac.compare_digest(p["mac"], hmac.new(ANSWER.encode(), "\n".join(lines).encode(), hashlib.sha256).hexdigest())


class Install(Base):
    def test_install_collects_the_key_and_shows_the_fixed_list_with_no_computer(self):
        v, pid = self.prompt("/datastash:install", entry="<command-name>/datastash:install</command-name>\n"
                             "<command-message>datastash:install</command-message>\n<command-args></command-args>")
        ai = v["hookSpecificOutput"]["additionalContext"]
        self.assertIn('ledger_agent_register with name "web-app"', ai)
        self.assertNotIn("datastash login", ai)
        self.tool("PostToolUse", "ledger_agent_register", {"name": "web-app"},
                  {"agent_id": OWN, "notes_tabs": [{"workbook_id": WB}]})
        bound = self.tool("PreToolUse", "ledger_agent_key", {"name": "web-app", "code_challenge": "ai-made"})
        challenge = bound["hookSpecificOutput"]["updatedInput"]["code_challenge"]
        self.assertNotEqual(challenge, "ai-made")
        self.answers[ds.COLLECT_PATH] = (200, {"agent_id": OWN, "name": "web-app", "key": KEY, "answer_key": ANSWER})
        out = self.tool("PostToolUse", "ledger_agent_key", {"name": "web-app"},
                        {"collect": "ready", "mint_id": MINT, "name": "web-app", "notice": "server words"})
        self.assertEqual(out["systemMessage"], ds.MINT_LINE.format(name="web-app"))
        sent = [c for c in self.calls if c[1] == ds.COLLECT_PATH][0][3]
        self.assertEqual(ds.challenge_of(sent["verifier"]), challenge)  # only the hash crossed the model
        self.assertEqual(sent["mint_id"], MINT)
        self.assertNotIn(KEY, json.dumps(out) + json.dumps(bound))
        self.assertFalse(os.path.exists(ds.verifier_path("agent-web-app")))
        for p in (ds.key_path("web-app"), ds._fill("{c}/agent-{s}.answer", "web-app")):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(ds.read_agent_answer("web-app"), ANSWER)
        self.tool("PostToolUse", "ledger_agents_list", {}, LIST)
        note = self.tool("PreToolUse", "ledger_agents_note", {"kind": "uninstall", "targets": [OWN]})
        got = note["hookSpecificOutput"]["updatedInput"]
        self.assertEqual((got["kind"], got["targets"]), ("install", sorted([ALPHA, BETA])))
        self.assertTrue(proof_ok(got["typed_proof"], "install", [ALPHA, BETA], pid, "/datastash:install"))
        done = self.tool("PostToolUse", "ledger_agents_note", got, {"run_id": "r1"})["systemMessage"]
        self.assertEqual(done, "datastash is on for your agents:\n- web-app (public)\n- alpha (private)\n- beta (public)\n"
                               "To remove one or many: /datastash:uninstall and we'll show you the list of agents.")

    def test_a_failed_collect_saves_nothing(self):
        self.prompt("/datastash:install")
        self.tool("PreToolUse", "ledger_agent_key", {"name": "web-app"})
        out = self.tool("PostToolUse", "ledger_agent_key", {"name": "web-app"}, {"collect": "ready", "mint_id": MINT})
        self.assertEqual(out["systemMessage"], ds.INSTALL_FAILED_LINE)
        self.assertFalse(os.path.exists(ds.key_path("web-app")))

    def test_install_notes_only_from_the_dedicated_field_sent_by_the_person(self):
        note = {"kind": "install", "sent_by": "person", "name": "web-app", "agent_id": OWN, "workbook_id": WB}
        out = self.tool("PostToolUse", "ledger_rows_read", {}, {"rows": [], ds.NOTE_FIELD: note})
        self.assertIn(f'ledger_agent_key now with name "web-app", agent_id "{OWN}"', out["hookSpecificOutput"]["additionalContext"])
        for body in ({ds.NOTE_FIELD: dict(note, sent_by="agent")}, {"rows": [{ds.NOTE_FIELD: note}]},
                     {"text": json.dumps({ds.NOTE_FIELD: note})}):
            self.assertIsNone(self.tool("PostToolUse", "ledger_rows_read", {}, body))
        self.installed()
        self.assertIsNone(self.tool("PostToolUse", "ledger_rows_read", {}, {ds.NOTE_FIELD: note}))  # already on

    def test_a_note_call_without_the_persons_command_is_refused(self):
        self.installed()
        out = self.tool("PreToolUse", "ledger_agents_note", {"kind": "uninstall", "targets": [ALPHA]})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


class Uninstall(Base):
    def setUp(self):
        super().setUp()
        self.installed()

    def show_list(self, text="/datastash:uninstall"):
        self.prompt(text)
        return self.tool("PostToolUse", "ledger_agents_list", {}, LIST)

    def test_the_list_then_typed_names(self):
        shown = self.show_list()["systemMessage"]
        self.assertEqual(shown, "Your datastash agents:\n- web-app (public)\n- alpha (private)\n- beta (public)\n"
                                + ds.PICK_LINE)
        v, pid = self.prompt("Beta")
        self.assertIn('kind "uninstall"', v["hookSpecificOutput"]["additionalContext"])
        got = self.tool("PreToolUse", "ledger_agents_note", {"targets": [OWN, ALPHA]})["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(got["targets"], [BETA])  # never the model's targets
        self.assertTrue(proof_ok(got["typed_proof"], "uninstall", [BETA], pid, "Beta"))
        again = self.tool("PreToolUse", "ledger_agents_note", {})  # used once
        self.assertEqual(again["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_all_in_one_go_removes_this_agent_last_and_says_who_is_unconfirmed(self):
        self.prompt("/datastash:uninstall all")
        self.tool("PostToolUse", "ledger_agents_list", {}, LIST)
        got = self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(got["targets"], sorted([OWN, ALPHA, BETA]))
        line = self.tool("PostToolUse", "ledger_agents_note", got, {"run_id": "r1", "confirmed": [BETA]})["systemMessage"]
        self.assertEqual(line, 'datastash is off for "web-app", "alpha", "beta". Removed: listener, key, settings and local '
                               'copies. Removed 3 agents. Not yet confirmed: "alpha" (they clean up when they next start). '
                               "Your datastash connector in Claude is unchanged.")
        self.assertFalse(os.path.exists(ds.CONF))  # the last agent here: the box goes too

    def test_model_typed_text_is_refused(self):
        self.show_list()
        self.prompt("all", source="sdk")  # a headless or model-made prompt: no person origin
        out = self.tool("PreToolUse", "ledger_agents_note", {})
        self.assertEqual(out["systemMessage"], ds.NOT_PERSON_LINE)
        self.assertTrue(os.path.exists(ds.key_path("web-app")))

    def test_look_alike_names_are_refused_and_other_prompts_pass(self):
        lst = dict(LIST, agents=LIST["agents"] + [{"agent_id": str(uuid.UUID(int=4)), "name": "Be ta", "private": False}])
        self.prompt("/datastash:uninstall")
        self.tool("PostToolUse", "ledger_agents_list", {}, lst)
        v, _ = self.prompt("beta")
        self.assertEqual((v["decision"], v["reason"]), ("block", ds.PICK_UNKNOWN_LINE))
        self.assertEqual(ds.fold_name("A1pha"), ds.fold_name("alpha"))
        self.assertIsNone(self.prompt("what was that about?")[0])  # not a pick: an ordinary prompt

    def test_this_agent_alone_ends_itself_on_its_own_key(self):
        self.answers[ds.UNINSTALL_SELF_PATH] = (200, {"ok": True})
        self.show_list()
        self.prompt("this")
        out = self.tool("PreToolUse", "ledger_agents_note", {})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn('datastash is off for "web-app"', out["systemMessage"])
        self.assertIn(("POST", ds.UNINSTALL_SELF_PATH, KEY, {"scope": "self"}), self.calls)


class Cleanup(Base):
    def walk(self, s):
        made = [ds._fill(t, s) for t in ds.AGENT_FILES]
        for p in made:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if not os.path.exists(p):
                ds.write_private(p, "x\n")
        return made

    def test_every_row_goes_and_nothing_else_until_the_last_agent(self):
        ds.save_agent("other", str(uuid.UUID(int=5)), KEY, WB, self.folder + "-2")
        self.installed()
        mine, theirs = self.walk("web-app"), self.walk("other")
        self.assertFalse(ds.remove_agent_here("web-app"))
        self.assertEqual([p for p in mine if os.path.exists(p)], [])
        self.assertEqual([p for p in theirs if not os.path.exists(p)], [])
        os.makedirs(ds.MAIN_DIR)
        ds.write_private(ds._fill(ds.BOX_FILES[0]), "x")
        self.kc.update({"refresh-token": "r", "answer-key": "a"})
        self.assertTrue(ds.remove_agent_here("other"))
        self.assertFalse(os.path.exists(ds.CONF))
        self.assertFalse(os.path.exists(ds._fill(ds.BOX_FILES[0])))
        self.assertTrue(os.path.isdir(ds.MAIN_DIR) and os.path.isdir(self.folder))  # the person's folders stay
        self.assertEqual(self.kc, {})
        self.assertFalse(ds.remove_agent_here("../x"))  # never a path from outside the table

    def test_the_answer_key_goes_to_the_keychain_when_there_is_one(self):
        self.kc_works = True
        self.installed()
        self.assertEqual((self.kc["agent-answer-web-app"], ds.read_agent_answer("web-app")), (ANSWER, ANSWER))
        self.assertFalse(os.path.exists(ds._fill("{c}/agent-{s}.answer", "web-app")))
        ds.remove_agent_here("web-app")
        self.assertEqual(self.kc, {})


class Removed410(Base):
    def listen(self, err):
        agent = dict(ds.read_json(ds.conf_path("web-app"), {}), slug="web-app")
        saved = {k: getattr(ds, k) for k in ("fetch_page", "start_beat", "start_hello", "start_status_refresher",
                                             "start_map_refresher", "_sleep")}
        self.addCleanup(lambda: [setattr(ds, k, v) for k, v in saved.items()])
        ds.fetch_page = lambda *a, **k: (_ for _ in ()).throw(err)
        ds.start_beat = ds.start_hello = ds.start_status_refresher = ds.start_map_refresher = lambda *a, **k: None
        ds._sleep = lambda *a: (_ for _ in ()).throw(KeyboardInterrupt)
        lines = []
        try:
            rc = ds._listen_loop(agent, "web-app", "s1", lines.append)
        except KeyboardInterrupt:
            rc = "kept polling"
        return rc, lines

    def test_agent_removed_confirms_with_the_dead_key_then_cleans_up(self):
        self.installed()
        self.answers[ds.UNINSTALL_CONFIRM_PATH] = (200, {"ok": True})
        self.assertEqual(self.listen(ds.ApiError(410, {"error": "agent_removed"})), (0, [ds.REMOVED_HERE_LINE]))
        self.assertIn(("POST", ds.UNINSTALL_CONFIRM_PATH, KEY, {}), self.calls)
        self.assertFalse(os.path.exists(ds.CONF))

    def test_a_plain_410_is_a_cursor_reset_not_a_removal(self):
        self.installed()
        self.assertEqual(self.listen(ds.ApiError(410, {"error": "cursor_expired"}))[0], "kept polling")
        self.assertTrue(os.path.exists(ds.key_path("web-app")))


class Commands(unittest.TestCase):
    def test_the_commands_are_the_persons_only_and_no_hook_starts_a_login(self):
        for n in ("install", "uninstall"):
            with open(os.path.join(HERE, "..", "plugins", "datastash", "commands", n + ".md")) as f:
                self.assertIn("\ndisable-model-invocation: true\n", f.read())
        self.assertFalse(ds.hooks_offer_sign_in())
        self.assertIsNone(ds.UNINSTALL_RE.fullmatch("please datastash uninstall"))
        self.assertEqual(ds.show_name("a\u202eb\x07c\n"), "abc")


if __name__ == "__main__":
    unittest.main()
