#!/usr/bin/env python3
"""Offline tests for XLI-390 P1 (plugin 1.0.0): /datastash:install and /datastash:uninstall.
- Install: this agent's key only through collect (the connector call carries the S256 challenge of a verifier made
  here; no code or key in any hook output), saved mode 600 (the answer key in the keychain or mode 600), then the
  fixed-format list (name and public or private, no computer) and the removal line.
- Uninstall: the list, then names or "all" typed by the person, signed over agent ids from the list id; model-typed
  text and a note with no command are refused; names that differ only by case or look-alike letters are refused.
- Cleanup: every row of the cleanup table goes, and nothing else; the box goes with its last agent.
- 410 agent_removed: the listener confirms with the dead key and cleans up by itself; a plain 410 does not.
- Review 1 (cycle 2): the slash commands pass the typed check in the real Claude Code 2.1.330 shape
  (test/fixtures/slash-command-2.1.330.jsonl and -hooks.json: copied from a real interactive session on a throwaway
  plugin, only renamed to datastash and given placeholder ids and paths) and nothing like it; the list's 'visibility', the
  note proof's exact shape and the note call's exact fields (X2 #467); install notes only from a datastash result's
  _meta.datastash_note (the hook's tool_response or the transcript's mcpMeta) and only for this agent's own id; the
  signer goes last on its own key with the run id; the uninstall turn's own hooks make no folder again; the rotated log
  and the workbook's status copy go; no login path is left.
No network; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_install.py"""
import hashlib
import hmac
import io
import json
import os
import re
import stat
import subprocess
import sys
import time
import unittest
from unittest import mock
import uuid
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import BIN, ds  # noqa: E402  (the same harness and plugin copy)
import harness  # noqa: E402

WB = "11111111-2222-3333-4444-555555555555"
OWN, ALPHA, BETA = (str(uuid.UUID(int=i)) for i in (1, 2, 3))
MINT = str(uuid.UUID(int=9))
LIST_ID, RUN = str(uuid.UUID(int=0xA1)), str(uuid.UUID(int=0xB2))
FIXTURES = os.path.join(HERE, "fixtures")
ROOT_PLUGIN = os.path.join(os.path.dirname(HERE), "plugins", "datastash")
KEY, ANSWER = "ldgr_ag_" + "k" * 40, "A" * 43
LIST = {"list_id": LIST_ID, "expires_in_seconds": 600,
        "agents": [{"agent_id": OWN, "name": "web-app", "visibility": "public"},
                   {"agent_id": ALPHA, "name": "alpha", "visibility": "private"},
                   {"agent_id": BETA, "name": "beta", "visibility": "public"}]}
TOOL = "mcp__claude_ai_datastash__"
OWN_TOOL = "mcp__plugin_datastash_datastash__"  # the plugin's own connector (https://getdatastash.com/mcp): trusted by URL
PATCHED = ("CONF", "STATE", "PENDING", "OFF", "MAIN_DIR", "http", "_claude_chain", "_kc", "_stop_listener", "_gone_path", "hook_input",
           "spawn_detached", "_temp_roots")


def real_entries(name):
    """The real Claude Code 2.1.330 transcript entries of one typed slash command (its own entry and its isMeta
    companion), from the fixture."""
    with open(os.path.join(FIXTURES, "slash-command-2.1.330.jsonl")) as f:
        rows = [json.loads(x) for x in f if x.strip()]
    own = [r for r in rows if isinstance(r["message"]["content"], str) and f"<command-name>/{name}</command-name>" in r["message"]["content"]][0]
    return own, [r for r in rows if r["promptId"] == own["promptId"] and r is not own][0]


class Base(unittest.TestCase):
    CONNECTOR_SIGNED_IN = False  # 1.0.23 (card 1): the connector-install tests below set it

    def setUp(self):
        self.saved = {k: getattr(ds, k) for k in PATCHED}
        root = os.path.realpath(harness.tmpdir(self, prefix="install-", dir=harness.ROOT))
        ds._temp_roots = lambda: []  # the throwaway folders live in a temp folder; test_setup.py's TempAndSystem checks the real list
        ds.CONF, ds.MAIN_DIR = os.path.join(root, "datastash"), os.path.join(root, "datastash main")
        ds.STATE, ds.PENDING, ds.OFF = (os.path.join(ds.CONF, d) for d in ("state", "pending", "off"))
        self.env = os.environ.get("DATASTASH_CONFIG_DIR")
        os.environ["DATASTASH_CONFIG_DIR"] = ds.CONF
        self.folder = os.path.join(root, "web-app")
        os.makedirs(self.folder)
        self.calls, self.answers, self.kc = [], {}, {}
        ds._claude_chain = lambda: [[4242, "start-A"]]
        ds._stop_listener = lambda s: self.calls.append(("stop", s))
        gone = os.path.join(root, "gone-tombstone")
        ds._gone_path = lambda: gone
        self.spawned = []
        ds.spawn_detached = lambda argv, cwd: self.spawned.append((argv, cwd))

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
        # 1.0.23 (card 1): these tests run the connector's install, on a computer whose connector is signed in; one that
        # is not signed in gets the email sign-in line instead (test/test_email_signin.py)
        if self.CONNECTOR_SIGNED_IN:
            real_signed_in = ds.computer_signed_in
            ds.computer_signed_in = lambda deep=False: True
            self.addCleanup(setattr, ds, "computer_signed_in", real_signed_in)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(ds, k, v)
        if self.env is None:
            os.environ.pop("DATASTASH_CONFIG_DIR", None)
        else:
            os.environ["DATASTASH_CONFIG_DIR"] = self.env

    def transcript(self, sid):
        d = os.path.join(ds.CLAUDE_DIR, "projects", "-install-" + self.id().rsplit(".", 1)[-1][:60])  # one per test
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

    def slash(self, text, sid="s1", **change):
        """The person types a slash command: the real hook input shape (fixture), then the real transcript entries
        Claude Code writes for it (its own entry and the isMeta companion), with `change` applied to its own entry."""
        name, _, args = text[1:].partition(" ")
        with open(os.path.join(FIXTURES, "slash-command-2.1.330-hooks.json")) as f:
            shapes = {h["prompt"].split(" ")[0]: h for h in json.load(f)}
        pid = str(uuid.uuid4())
        inp = dict(shapes["/datastash:uninstall" if "uninstall" in name else "/datastash:install"], session_id=sid,
                   prompt=text, cwd=self.folder, prompt_id=pid, transcript_path=self.transcript(sid))
        v = self.hook(ds.cmd_prompt, inp)
        own, meta = real_entries(name if name in ("datastash:install", "datastash:uninstall") else "datastash:install")
        c = f"<command-message>{name}</command-message>\n<command-name>/{name}</command-name>"
        own = dict(own, promptId=pid, sessionId=sid, message={"role": "user", "content": c + (
            f"\n<command-args>{args}</command-args>" if args else "")},
                   timestamp=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()))
        for k, val in change.items():
            if val is None:
                own.pop(k, None)
            else:
                own[k] = val
        with open(self.transcript(sid), "a") as f:
            f.write(json.dumps(own) + "\n" + json.dumps(dict(meta, promptId=pid, sessionId=sid)) + "\n")
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


def proof_ok(p, kind, targets, pid, typed, own=OWN, answer=ANSWER):
    """X2's checkNoteProof, line for line: exactly these 7 fields, and the MAC over its 9-line proofInput."""
    if sorted(p) != ["agent_id", "iat", "mac", "prompt_id", "typed_at", "typed_sha256", "v"] or p["v"] != 1:
        return False
    if p["agent_id"] != own or p["prompt_id"] != pid or not 0 <= p["iat"] - p["typed_at"] <= 125000:
        return False
    lines = ["datastash-agents-note/v1", own, LIST_ID.lower(), kind, ",".join(sorted(t.lower() for t in targets)), pid,
             hashlib.sha256(typed.encode()).hexdigest(), str(p["typed_at"]), str(p["iat"])]
    return hmac.compare_digest(p["mac"], hmac.new(answer.encode(), "\n".join(lines).encode(), hashlib.sha256).hexdigest())


class Install(Base):
    def test_install_collects_the_key_and_shows_the_fixed_list_with_no_computer(self):
        v, pid = self.slash("/datastash:install")
        ai = v["hookSpecificOutput"]["additionalContext"]
        self.assertIn('ledger_agent_register with name "web-app"', ai)
        self.assertNotIn("datastash login", ai)
        self.tool("PostToolUse", "ledger_agent_register", {"name": "web-app"},
                  {"agent_id": OWN, "notes_tabs": [{"workbook_id": WB}]})
        bound = self.tool("PreToolUse", "ledger_agent_key", {"name": "web-app", "code_challenge": "ai-made"})
        challenge = bound["hookSpecificOutput"]["updatedInput"]["code_challenge"]
        self.assertNotEqual(challenge, "ai-made")
        self.answers[ds.COLLECT_PATH] = (200, {"agent_id": OWN, "name": "web-app", "workbook_id": WB, "key": KEY,
                                               "answer_key": ANSWER})
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
        self.assertEqual(sorted(got), ["kind", "list_id", "targets", "typed_proof"])  # X2: additionalProperties false
        self.assertEqual((got["kind"], got["list_id"], got["targets"]), ("install", LIST_ID, sorted([ALPHA, BETA])))
        self.assertTrue(proof_ok(got["typed_proof"], "install", [ALPHA, BETA], pid, "/datastash:install"))
        done = self.tool("PostToolUse", "ledger_agents_note", got, {"run_id": RUN, "kind": "install", "targets": 2,
                                                                     "status_path": "/api/v1/agent/uninstall/" + RUN})["systemMessage"]
        self.assertEqual(done, ds._plain_typed("Done. Datastash is on for web-app. Time per card is on. To turn it off, type: **datastash "
                               "time-per-card off**\n"
                               "datastash is on for your agents:\n- web-app (public)\n- alpha (private)\n- beta (public)\n"
                               "To remove one or many: **/datastash:uninstall** and we'll show you the list of agents.\n"
                               "To make one private: say **make <name> private**.\n"
                               "To make one public: say **make <name> public**.\n" + ds.SYNC_TYPE_LINE))

    def test_the_only_agent_gets_the_list_with_no_note(self):
        self.installed()
        self.slash("/datastash:install")
        out = self.tool("PostToolUse", "ledger_agents_list", {}, dict(LIST, agents=LIST["agents"][:1]))
        text = ("Done. Datastash is on for web-app. " + ds.TPC_ON_LINE + "\ndatastash is on for your agents:\n- web-app "
                "(public)\n" + ds.REMOVE_LINE + "\n" + ds.PRIVATE_LINE + "\n" + ds.PUBLIC_LINE + "\n" + ds.SYNC_TYPE_LINE)
        self.assertEqual(out["systemMessage"], ds._plain_typed(text))
        self.assertIn('Type "datastash sync" to keep', out["systemMessage"])  # 1.0.18: no markdown on that surface
        self.assertEqual(out["hookSpecificOutput"]["updatedMCPToolOutput"], [{"type": "text", "text": text}])
        self.assertIn(text, out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_a_list_without_visibility_shows_private(self):
        lst = ds.norm_list({"list_id": LIST_ID.upper(), "agents": [{"agent_id": ALPHA, "name": "a"},
                                                                  {"agent_id": BETA, "name": "b", "visibility": "public"}]})
        self.assertEqual(lst["list_id"], LIST_ID)
        self.assertEqual([a["shut"] for a in lst["agents"]], [True, False])
        self.assertIsNone(ds.norm_list({"list_id": "list-1", "agents": []}))  # X2's list ids are uuids

    def test_a_failed_collect_saves_nothing(self):
        self.prompt("/datastash:install")
        self.tool("PreToolUse", "ledger_agent_key", {"name": "web-app"})
        out = self.tool("PostToolUse", "ledger_agent_key", {"name": "web-app"}, {"collect": "ready", "mint_id": MINT})
        self.assertEqual(out["systemMessage"], ds._plain_typed(ds.INSTALL_FAILED_LINE))
        self.assertFalse(os.path.exists(ds.key_path("web-app")))

    def result_entry(self, sid, tool, note, sidechain=False, tid=None):
        """A datastash connector call and its result as Claude Code writes them to the transcript (after the
        PostToolUse hook): the assistant's tool_use, then the user entry whose mcpMeta holds the server's _meta."""
        tid = tid or "toolu_" + uuid.uuid4().hex[:20]
        call = {"type": "assistant", "isSidechain": sidechain, "uuid": str(uuid.uuid4()), "message": {
            "role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": tool, "input": {"agent_id": OWN}}]}}
        res = {"type": "user", "isSidechain": sidechain, "uuid": str(uuid.uuid4()), "sourceToolAssistantUUID": call["uuid"],
               "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                       "content": [{"type": "text", "text": "{}"}]}]},
               "toolUseResult": [{"type": "text", "text": "{}"}], "mcpMeta": {"_meta": {ds.NOTE_FIELD: note}}}
        with open(self.transcript(sid), "a") as f:
            f.write(json.dumps(call) + "\n" + json.dumps(res) + "\n")

    def post(self, sid="s1", tool="ledger_rows_read", response=None, via=OWN_TOOL):
        inp = {"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": via + tool, "tool_input": {},
               "cwd": self.folder, "transcript_path": self.transcript(sid),
               "tool_response": response if response is not None else [{"type": "text", "text": "{}"}]}
        return self.hook(ds.cmd_agent_tool, inp)

    def known_no_key(self):
        """This folder's agent is known here (its record and id) but has no key yet: the agent an install note is for."""
        ds.save_agent("web-app", OWN, KEY, WB, self.folder)
        ds._drop(ds.key_path("web-app"))

    def test_install_note_only_from_a_datastash_results_meta_and_only_for_this_agents_own_id(self):
        self.known_no_key()
        note = {"note_id": str(uuid.UUID(int=0xC1)), "run_id": RUN, "kind": "install", "agent_id": OWN, "sent_by": "person"}
        self.assertIsNone(self.post())  # nothing in the transcript yet: Claude Code writes the result after this hook
        self.result_entry("s1", OWN_TOOL + "ledger_rows_read", note)
        ai = self.post()["hookSpecificOutput"]["additionalContext"]  # the next hook sees it in mcpMeta
        self.assertIn(f'ledger_agent_key now with name "web-app", agent_id "{OWN}" and only_if_no_key true', ai)
        self.assertIsNone(self.post())  # each note once

    def test_the_hooks_own_tool_response_meta_counts_too(self):
        self.known_no_key()
        note = {"note_id": str(uuid.UUID(int=0xC2)), "run_id": RUN, "kind": "install", "agent_id": OWN, "sent_by": "person"}
        out = self.post(response={"content": [{"type": "text", "text": "{}"}], "_meta": {ds.NOTE_FIELD: note}})
        self.assertIn("ledger_agent_key now", out["hookSpecificOutput"]["additionalContext"])

    def test_a_note_for_another_agent_or_from_anywhere_else_does_nothing(self):
        self.known_no_key()
        good = {"run_id": RUN, "kind": "install", "agent_id": OWN, "sent_by": "person"}
        n = iter(range(0xD0, 0xE0))
        nid = lambda: str(uuid.UUID(int=next(n)))  # noqa: E731
        self.result_entry("s1", OWN_TOOL + "ledger_rows_read", dict(good, note_id=nid(), agent_id=ALPHA))  # another agent's
        self.result_entry("s1", OWN_TOOL + "ledger_rows_read", dict(good, note_id=nid(), sent_by="agent"))
        self.result_entry("s1", OWN_TOOL + "ledger_rows_read", dict(good, note_id=nid(), kind="uninstall"))
        self.result_entry("s1", OWN_TOOL + "ledger_rows_read", dict(good, note_id=nid()), sidechain=True)  # a subagent's
        self.result_entry("s1", "mcp__claude_ai_other__read", dict(good, note_id=nid()))  # not a datastash call
        self.assertIsNone(self.post())
        # the result's text, a row or a message never counts: only the _meta field
        for body in ({ds.NOTE_FIELD: dict(good, note_id=nid())}, {"rows": [{ds.NOTE_FIELD: dict(good, note_id=nid())}]}):
            self.assertIsNone(self.post(response=[{"type": "text", "text": json.dumps(body)}]))
        self.installed()  # already has its key: nothing to do
        self.result_entry("s1", OWN_TOOL + "ledger_rows_read", dict(good, note_id=nid()))
        self.assertIsNone(self.post())

    def test_a_look_alike_connector_named_datastash_never_gives_an_install_note(self):
        """XLI-391 review item 1: trusted only by its configured URL (https, getdatastash.com exactly), never by name."""
        self.known_no_key()
        with open(os.path.join(self.folder, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": {"datastash": {"type": "http", "url": "https://getdatastash.com.evil.example/mcp"},
                                      "datastash-sub": {"type": "http", "url": "https://x.getdatastash.com/mcp"},
                                      "datastash-http": {"type": "http", "url": "http://getdatastash.com/mcp"},
                                      "mine": {"type": "http", "url": "https://getdatastash.com/mcp"}}}, f)
        good = {"run_id": RUN, "kind": "install", "agent_id": OWN, "sent_by": "person"}
        n = iter(range(0xE0, 0xF0))
        nid = lambda: str(uuid.UUID(int=next(n)))  # noqa: E731
        for via in ("mcp__datastash__", "mcp__datastash-sub__", "mcp__datastash-http__", TOOL):  # TOOL: no URL seen here
            self.result_entry("s1", via + "ledger_rows_read", dict(good, note_id=nid()))
            self.assertIsNone(self.post(via=via), via)
            meta = {"content": [{"type": "text", "text": "{}"}], "_meta": {ds.NOTE_FIELD: dict(good, note_id=nid())}}
            self.assertIsNone(self.post(via=via, response=meta), via)
        out = self.post(via="mcp__mine__", response={"content": [], "_meta": {ds.NOTE_FIELD: dict(good, note_id=nid())}})
        self.assertIn("ledger_agent_key now", out["hookSpecificOutput"]["additionalContext"])  # added by its URL: counts

    def test_a_note_at_the_turns_end_is_picked_up_by_the_stop_hook(self):
        self.known_no_key()
        self.result_entry("s1", OWN_TOOL + "ledger_rows_read",
                          {"note_id": str(uuid.UUID(int=0xC3)), "run_id": RUN, "kind": "install", "agent_id": OWN, "sent_by": "person"})
        stop = {"session_id": "s1", "hook_event_name": "Stop", "cwd": self.folder, "transcript_path": self.transcript("s1")}
        out = self.hook(ds.cmd_status_check, stop)
        # XLI-443: never a Stop block the person sees; the agent gets it at its next prompt or tool call
        self.assertNotEqual((out or {}).get("decision"), "block")
        self.assertNotIn("ledger_agent_key", json.dumps(out or {}))
        self.assertIn("ledger_agent_key now", ds.nudges_take("s1"))
        self.assertEqual(ds.nudges_take("s1"), "")
        self.assertIsNone(self.hook(ds.cmd_status_check, dict(stop, stop_hook_active=True)))

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
                                + ds._plain_typed(ds.PICK_LINE))
        v, pid = self.prompt("Beta")
        self.assertIn('kind "uninstall"', v["hookSpecificOutput"]["additionalContext"])
        got = self.tool("PreToolUse", "ledger_agents_note", {"targets": [OWN, ALPHA]})["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(got["targets"], [BETA])  # never the model's targets
        self.assertTrue(proof_ok(got["typed_proof"], "uninstall", [BETA], pid, "Beta"))
        self.assertEqual(sorted(got), ["kind", "list_id", "targets", "typed_proof"])
        again = self.tool("PreToolUse", "ledger_agents_note", {})  # used once
        self.assertEqual(again["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_all_in_one_go_removes_this_agent_last_and_says_who_is_unconfirmed(self):
        v, _ = self.slash("/datastash:uninstall all")  # the real slash-command entry, with args
        self.assertIsNone(v.get("decision"))
        shown = self.tool("PostToolUse", "ledger_agents_list", {}, LIST)
        self.assertEqual(shown["systemMessage"], "This removes all 3 of your datastash agents:\n- web-app (public)\n"
                         "- alpha (private)\n- beta (public)\n" + ds._plain_typed(ds.ALL_CONFIRM_LINE.format(n=3)))
        self.assertTrue(shown["hookSpecificOutput"]["additionalContext"].startswith(ds.ALL_CONFIRM_AI))
        self.assertEqual(self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["permissionDecision"],
                         "deny")  # "all" alone removes nothing yet
        v, pid = self.prompt("remove all 3")
        self.assertIn('kind "uninstall"', v["hookSpecificOutput"]["additionalContext"])
        got = self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(got["targets"], sorted([OWN, ALPHA, BETA]))
        self.assertTrue(proof_ok(got["typed_proof"], "uninstall", [OWN, ALPHA, BETA], pid, "remove all 3"))
        status = "/api/v1/agent/uninstall/" + RUN
        self.answers[status] = (200, {"run_id": RUN, "kind": "uninstall", "total": 3, "confirmed": ["beta"],
                                      "not_confirmed": ["alpha", "web-app"]})
        self.answers[ds.UNINSTALL_SELF_PATH] = (200, {"result": "removed", "run_id": RUN})
        line = self.tool("PostToolUse", "ledger_agents_note", got, {
            "run_id": RUN, "kind": "uninstall", "targets": 3, "removed": ["alpha", "beta"], "this_agent_last": True,
            "status_path": status})["systemMessage"]
        self.assertIn(("GET", status, KEY, None), self.calls)  # who confirmed, on this agent's still-live key
        self.assertIn(("POST", ds.UNINSTALL_SELF_PATH, KEY, {"run_id": RUN}), self.calls)  # then this agent, last
        self.assertLess(self.calls.index(("GET", status, KEY, None)), self.calls.index(("POST", ds.UNINSTALL_SELF_PATH, KEY, {"run_id": RUN})))
        self.assertEqual(line, "Done. Datastash is uninstalled from web-app, alpha and beta. Still to clean up on their own "
                               "computers when they next start: alpha. Your datastash connector in Claude is unchanged.")
        self.assertFalse(os.path.exists(ds.CONF))  # the last agent here: the box goes too

    def test_model_typed_text_is_refused(self):
        self.show_list()
        self.prompt("alpha", source="sdk")  # a headless or model-made prompt: no person origin
        out = self.tool("PreToolUse", "ledger_agents_note", {})
        self.assertEqual(out["systemMessage"], ds.NOT_PERSON_LINE)
        self.assertTrue(os.path.exists(ds.key_path("web-app")))

    def test_look_alike_names_are_refused_and_other_prompts_pass(self):
        lst = dict(LIST, agents=LIST["agents"] + [{"agent_id": str(uuid.UUID(int=4)), "name": "Be ta", "visibility": "public"}])
        self.prompt("/datastash:uninstall")
        self.tool("PostToolUse", "ledger_agents_list", {}, lst)
        v, _ = self.prompt("beta")
        self.assertEqual(v["decision"], "block")  # 1.0.11 final: "Be ta" is an old-style name; "beta" may stand for it
        self.assertTrue(v["reason"].startswith('Not done: "beta" may stand for your agent "Be ta", which has an old-style name'),
                        v["reason"])
        self.assertIn("Nothing was removed.", v["reason"])
        self.assertEqual(ds.fold_name("A1pha"), ds.fold_name("alpha"))
        self.assertIsNone(self.prompt("what was that about?")[0])  # not a pick: an ordinary prompt

    def test_this_agent_alone_ends_itself_on_its_own_key(self):
        self.answers[ds.UNINSTALL_SELF_PATH] = (200, {"ok": True})
        self.show_list()
        _, pid = self.prompt("this")
        out = self.tool("PreToolUse", "ledger_agents_note", {})
        # 1.0.16: never a denied call on success: the note goes out signed, and its own answer carries the result
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        got = out["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(got["targets"], [OWN])
        self.assertTrue(proof_ok(got["typed_proof"], "uninstall", [OWN], pid, "this"))
        done = self.tool("PostToolUse", "ledger_agents_note", got, {"run_id": RUN, "kind": "uninstall", "targets": 1,
                                                                     "removed": [], "this_agent_last": True})
        line = "Done. Datastash is uninstalled from web-app. Your datastash connector in Claude is unchanged."
        self.assertEqual(done["systemMessage"], line)
        self.assertEqual(done["hookSpecificOutput"]["updatedMCPToolOutput"], [{"type": "text", "text": line}])
        self.assertIn(line, done["hookSpecificOutput"]["additionalContext"])
        self.assertIn(("POST", ds.UNINSTALL_SELF_PATH, KEY, {"run_id": RUN}), self.calls)

    def test_this_agent_with_no_answer_key_still_ends_itself(self):
        self.answers[ds.UNINSTALL_SELF_PATH] = (200, {"ok": True})
        ds._kc("drop", "agent-answer-web-app")
        ds._drop(ds._fill("{c}/agent-{s}.answer", "web-app"))
        self.show_list()
        self.prompt("this")
        out = self.tool("PreToolUse", "ledger_agents_note", {})
        self.assertEqual(out["systemMessage"], "Done. Datastash is uninstalled from web-app. Your datastash connector "
                         "in Claude is unchanged.")
        self.assertIn(("POST", ds.UNINSTALL_SELF_PATH, KEY, {}), self.calls)


AX, AX2, AX3 = (str(uuid.UUID(int=i)) for i in (0x51, 0x52, 0x53))
AX_LIST = dict(LIST, agents=LIST["agents"] + [{"agent_id": AX, "name": "alex", "visibility": "public"},
                                              {"agent_id": AX2, "name": "alex-2", "visibility": "public"}])
A, B, C, BC, SP = (str(uuid.UUID(int=i)) for i in (0x61, 0x62, 0x63, 0x64, 0x65))


def plain(*names, ids=None):
    """A list with these names only (ids in order, made up)."""
    ids = ids or [str(uuid.UUID(int=0x70 + i)) for i in range(len(names))]
    return {"list_id": LIST_ID, "expires_in_seconds": 600,
            "agents": [{"agent_id": i, "name": n, "visibility": "public"} for i, n in zip(ids, names)]}


ABC_LIST = plain("a", "b", "c", "bc", ids=[A, B, C, BC])


def picks_of(text, lst, own=None, reserved=True):
    return ds.resolve_picks(text, ds.norm_list(lst), own, reserved)


class StrictNames(Base):
    """1.0.11 review 1: ONE strict parser for every command that takes agent names. Tokens split at whitespace, commas and
    "and"; a name with spaces only in double quotes; never joined, never a prefix or a fuzzy match; each token equals
    exactly one listed name ignoring case; "all" only alone; any unknown or ambiguous token refuses the whole command."""
    def setUp(self):
        super().setUp()
        self.installed()

    def picks(self, args, lst=AX_LIST):
        self.slash("/datastash:uninstall " + args)
        return self.tool("PostToolUse", "ledger_agents_list", {}, lst)

    def targets(self):
        return self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["updatedInput"]["targets"]

    def refused(self, out, word):
        self.assertTrue(out["systemMessage"].startswith(f'Not done: "{word}"'), out["systemMessage"])
        self.assertIn("Nothing was removed.", out["systemMessage"])
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], ds.PICK_REFUSED_AI)
        self.assertEqual(self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["permissionDecision"], "deny")

    # reviewer CRITICAL 1: tokens are never joined into a longer name
    def test_a_b_c_with_bc_listed_removes_exactly_a_b_c(self):
        self.picks("a b c", ABC_LIST)
        self.assertEqual(sorted(self.targets()), sorted([A, B, C]))
        self.assertNotIn(BC, picks_of("a b c", ABC_LIST))

    def test_b_c_never_becomes_bc_and_bc_never_becomes_b_c(self):
        self.assertEqual(sorted(picks_of("b c", ABC_LIST)), sorted([B, C]))
        self.assertEqual(picks_of("bc", ABC_LIST), [BC])

    def test_alpha_beta_never_becomes_alpha_beta(self):
        lst = plain("alpha", "beta", "alpha-beta", ids=[A, B, C])
        self.assertEqual(sorted(picks_of("alpha beta", lst)), sorted([A, B]))
        self.assertEqual(picks_of("alpha-beta", lst), [C])

    # reviewer HIGH 1: "all" only as the single token
    def test_x_all_is_refused_and_removes_nothing(self):
        for text in ("alex all", "all alex", "alex, all", "alex and all", "all all", "alex alex-2 all"):
            self.assertIsNone(picks_of(text, AX_LIST), text)
        self.refused(self.picks("alex all"), "all")

    def test_all_typed_after_the_list_next_to_a_name_is_refused(self):
        self.prompt("/datastash:uninstall")
        self.tool("PostToolUse", "ledger_agents_list", {}, AX_LIST)
        v, _ = self.prompt("alex all")
        self.assertEqual(v["decision"], "block")
        self.assertTrue(v["reason"].startswith('Not done: "all" must be typed alone'))
        self.assertIn("Nothing was removed.", v["reason"])

    def test_all_alone_is_every_agent_and_this_alone_is_this_agent(self):
        self.assertEqual(picks_of("all", AX_LIST), "confirm")  # stored-name HIGH: "all" alone asks to confirm first
        self.assertEqual(picks_of("  ALL  ", AX_LIST), "confirm")
        self.assertEqual(sorted(ds.all_confirm(ds.norm_list(AX_LIST))["ids"]), sorted([OWN, ALPHA, BETA, AX, AX2]))
        self.assertEqual(picks_of("this", AX_LIST, OWN), [OWN])
        self.assertIsNone(picks_of("this alex", AX_LIST, OWN))
        self.assertNotIsInstance(picks_of("a ll", AX_LIST), list)  # reviewer LOW 2: never folded into "all"
        self.assertIsNone(picks_of("alex a ll", AX_LIST))

    # hyphenated pairs
    def test_hyphenated_pair_by_space_comma_and(self):
        for text in ("alex alex-2", "alex-2 alex", "alex,alex-2", "alex-2 and alex", "alex ,  alex-2\n"):
            self.assertEqual(sorted(picks_of(text, AX_LIST)), sorted([AX, AX2]), text)
        self.picks("alex alex-2")
        self.assertEqual(sorted(self.targets()), sorted([AX, AX2]))

    def test_each_hyphenated_name_alone_is_only_that_agent(self):
        self.assertEqual(picks_of("alex", AX_LIST), [AX])
        self.assertEqual(picks_of("alex-2", AX_LIST), [AX2])
        self.assertIsNone(picks_of("alex 2", AX_LIST))  # "2" is no name: never joined to alex-2
        self.assertIsNone(picks_of("alex alex2", AX_LIST))  # no folding of the dash

    def test_never_a_prefix_or_fuzzy_match(self):
        for text in ("ale", "alex-", "alex-3", "alexx", "a1ex", "web", "webapp"):
            self.assertNotIsInstance(picks_of(text, AX_LIST), list, text)
        self.refused(self.picks("alex alex-3"), "alex-3")

    # names that contain spaces (1.0.11 final: an old-style name, never picked by name, quoted or not)
    def test_a_quoted_name_with_spaces_never_counts(self):
        lst = plain("Be ta", "x", ids=[SP, A])
        for text in ('"be ta" x', "“Be ta”", '"Be ta"'):
            self.assertIsNone(picks_of(text, lst), text)
        self.assertEqual(picks_of('"x"', lst), [A])
        self.assertEqual(picks_of("all", lst), "confirm")

    def test_a_name_with_spaces_unquoted_never_counts(self):
        lst = plain("Be ta", "x", ids=[SP, A])
        self.assertIsNone(picks_of("be ta", lst))
        self.assertIsNone(picks_of("be ta x", lst))
        self.assertIsNone(picks_of('"be  ta" "be"', lst))

    def test_an_unclosed_quote_refuses_the_whole_command(self):
        out = self.picks('alex "alex-2')
        self.assertTrue(out["systemMessage"].startswith("Not done: a quote is not closed"))
        self.assertIn("Nothing was removed.", out["systemMessage"])
        self.assertEqual(self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["permissionDecision"], "deny")

    # case differences
    def test_case_differences_match_and_case_twins_are_refused(self):
        self.assertEqual(sorted(picks_of("ALEX Alex-2", AX_LIST)), sorted([AX, AX2]))
        twins = plain("Alex", "alex", ids=[A, B])
        self.assertIsNone(picks_of("alex", twins))
        self.refused(self.picks("ALEX", twins), "ALEX")

    def test_look_alike_twins_are_refused(self):
        lst = plain("beta", "Be-ta", ids=[A, B])
        self.assertIsNone(picks_of("beta", lst))
        self.assertIsNone(picks_of("Be-ta", lst))

    # duplicate tokens
    def test_duplicate_tokens_pick_the_agent_once(self):
        self.assertEqual(picks_of("alex alex ALEX, alex", AX_LIST), [AX])
        self.picks("alex-2 alex alex-2")
        self.assertEqual(sorted(self.targets()), sorted([AX, AX2]))

    # an empty input
    def test_an_empty_input_picks_nothing(self):
        for text in ("", "   ", ",,", "and", " , and \n"):
            self.assertEqual(picks_of(text, AX_LIST), "none", repr(text))

    def test_an_empty_quoted_name_is_refused(self):
        self.assertIsNone(picks_of('alex ""', AX_LIST))

    # any unknown token refuses the whole command
    def test_one_unknown_token_refuses_all_of_it_and_names_it(self):
        out = self.picks("alex alex-2 nobody")
        self.assertTrue(out["systemMessage"].startswith('Not done: "nobody" is not on the list'))
        self.refused(out, "nobody")

    def test_a_single_unknown_name_on_the_slash_line_names_it(self):  # reviewer LOW 1
        out = self.picks("alexis")
        self.assertTrue(out["systemMessage"].startswith('Not done: "alexis" is not on the list (closest exact names: "alex"'))
        self.refused(out, "alexis")

    def test_refusal_line_names_closest_exact_names(self):
        lst = ds.norm_list(dict(AX_LIST, agents=AX_LIST["agents"] + [{"agent_id": AX3, "name": "al-ex", "visibility": "public"}]))
        self.assertEqual(ds.pick_refused_line("alex-2 alex", lst, None), ds.PICK_REFUSED_LINE.format(
            why='"alex" matches more than one agent (closest exact names: "alex", "al-ex"); type one in double quotes exactly as the list shows it'))

    def test_a_refused_typed_pick_after_the_list_names_the_token(self):
        self.prompt("/datastash:uninstall")
        self.tool("PostToolUse", "ledger_agents_list", {}, AX_LIST)
        v, _ = self.prompt("alex, nobody-here")
        self.assertEqual(v["decision"], "block")
        self.assertTrue(v["reason"].startswith('Not done: "nobody-here" is not on the list'))
        self.assertIn("Nothing was removed.", v["reason"])

    # reviewer HIGH 2: public/private share the parser and never take "all"
    def test_make_alpha_all_public_changes_nothing(self):
        self.prompt("make alpha all public")
        out = self.tool("PostToolUse", "ledger_agents_list", {}, LIST)
        self.assertTrue(out["systemMessage"].startswith('Not done: "all" is not a name here'), out.get("systemMessage"))
        self.assertIn("Nothing was changed.", out["systemMessage"])
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], ds.VIS_REFUSED_AI)
        self.assertIn('Say only: "Nothing was changed."', ds.VIS_REFUSED_AI)
        self.assertEqual(self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_visibility_names_are_strict_too(self):
        lst = ds.norm_list(AX_LIST)
        self.assertEqual(ds.resolve_picks("alex alex-2", lst, None, reserved=False), sorted([AX, AX2], key=[AX, AX2].index))
        for text in ("alex all", "all alex", "this alex", "alex nobody", "ale"):
            self.assertNotIsInstance(ds.resolve_picks(text, lst, None, reserved=False), list, text)
        self.assertEqual(ds.resolve_picks("all", lst, None, reserved=False), "none")
        self.assertEqual(ds.resolve_picks("this function", lst, None, reserved=False), "none")

    def test_make_alex_nobody_public_names_the_token(self):
        self.prompt("make alex nobody public")
        out = self.tool("PostToolUse", "ledger_agents_list", {}, AX_LIST)
        self.assertTrue(out["systemMessage"].startswith('Not done: "nobody" is not on the list'))
        self.assertIn("Nothing was changed.", out["systemMessage"])
        self.assertEqual(self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_no_real_handle_in_any_tracked_file(self):
        # only a hash of the handle lives here, so the guard itself never holds the name
        bad = "789c1b07ed0d59b33beadb403abb2f3468c45fc87aa63043a68c3b0ee39ea1b3"
        root = os.path.realpath(os.path.join(HERE, ".."))
        try:
            rels = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, check=True).stdout.decode().split("\0")
        except (OSError, subprocess.CalledProcessError):
            rels = [os.path.relpath(os.path.join(d, n), root) for d, _, ns in os.walk(root) if "/.git" not in d for n in ns]
        seen = 0
        for rel in filter(None, rels):
            try:
                with open(os.path.join(root, rel), encoding="utf-8") as f:
                    text = f.read().casefold()
            except (OSError, UnicodeDecodeError):
                continue
            seen += 1
            for word in set(re.findall(r"[a-z0-9]+", text)):
                self.assertNotEqual(hashlib.sha256(word.encode()).hexdigest(), bad, rel)
        self.assertGreater(seen, 10)


KEPT, X, Y = (str(uuid.UUID(int=i)) for i in (0x81, 0x82, 0x83))
WORDS = ("all", "this", "and")
# spellings that read as a pick word: case, spaces, dashes, dots, look-alike digits, fullwidth and Cyrillic letters
WORD_SPELLINGS = ("all", "ALL", " All ", "a-ll", "A L L", "a.l.l", "a11", "ａｌｌ", "\u0430ll", "this", "THIS", "th1s",
                  "Th-is", "thi5", "and", "AND", "a n d", "\u0430nd")
NOT_WORDS = ("alley", "all-2", "all-agent", "this-app", "thistle", "android", "band", "andy", "ball", "web-app")


def with_kept(name, *others):
    """A list where one agent (KEPT) already has `name` (made before names were checked), beside web-app and others."""
    return plain(name, "web-app", *others, ids=[KEPT, OWN] + [X, Y][:len(others)])


class ReservedWords(Base):
    """1.0.11 review 2 HIGH: "all", "this" and "and" are words of the name commands, never agent names. Every naming path
    refuses them (or picks the next free name with the install suffix rule, -2), and an agent that already has such a name
    is never picked by name: the command is refused and says the agent must be renamed first."""
    def setUp(self):
        super().setUp()
        self.installed()

    def picks(self, args, lst):
        self.slash("/datastash:uninstall " + args)
        return self.tool("PostToolUse", "ledger_agents_list", {}, lst)

    def denied(self):
        return self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["permissionDecision"] == "deny"

    def kept_refusal(self, line, word, name):
        self.assertIn(f'"{word}" is a word this command keeps, never a name, and your agent "{name}" is named like it',
                      line)
        self.assertTrue(line.startswith(("Not done: ", '"')), line)
        self.assertIn("so that agent can't be picked by name", line)
        self.assertNotIn("agents page", line)  # stored-name HIGH: the agents page is gone (410)
        if line.startswith("Not done: "):
            self.assertTrue(line.endswith(ds._plain_typed(ds.OLD_STYLE_LINE)), line)

    def mkdir(self, name):
        d = os.path.join(os.path.dirname(self.folder), name)
        os.makedirs(d, exist_ok=True)
        return d

    # 1. the words and their spellings
    def test_every_spelling_of_a_pick_word_is_reserved(self):
        for n in WORD_SPELLINGS:
            self.assertIsNotNone(ds.pick_word_like(n), repr(n))
            self.assertTrue(ds.name_reserved(n), repr(n))
        for n in NOT_WORDS:
            self.assertIsNone(ds.pick_word_like(n), n)
            self.assertFalse(ds.name_reserved(n), n)
        for n in ("person", "Datastash", "@feedback"):  # the older reserved names stay reserved
            self.assertTrue(ds.name_reserved(n), n)

    def test_every_word_the_parser_treats_specially_is_reserved(self):
        self.assertEqual(set(ds._RESERVED) | {"and"}, set(ds.PICK_WORDS))

    # 2. every naming path
    def test_install_folder_named_a_pick_word_gets_the_next_free_name(self):
        for word in ("all", "This", "and", "a-ll", "ALL"):
            self.assertEqual(ds._free_name(self.mkdir(word)), word.lower() + "-2", word)  # names are lowercase
            self.assertEqual(ds._free_name(self.mkdir(word), prefer=word), word.lower() + "-2", word)

    def test_auto_name_never_gives_a_pick_word(self):
        for word in WORDS + ("AND",):
            kind, name = ds.auto_name(self.mkdir(word), None)
            self.assertEqual((kind, name), ("name", word.lower() + "-2"), word)
            kind, name = ds.auto_name(self.mkdir("plain-" + word), None, session_name=ds._folder_name(word))
            self.assertEqual((kind, name), ("name", word.lower() + "-2"), word)  # claude --agent <word>

    def test_register_sends_the_next_free_name(self):
        rid = str(uuid.UUID(int=0x91))
        self.answers[ds.AGENT_REGISTER_PATH] = (200, {"agent_id": rid, "name": "all-2"})
        ds._register_once("tok", self.mkdir("all"))
        sent = [c[3]["name"] for c in self.calls if c[1] == ds.AGENT_REGISTER_PATH]
        self.assertTrue(sent, self.calls)
        self.assertNotIn("all", sent)
        self.assertEqual(sent[0], "all-2")

    def test_install_command_in_a_folder_named_all_names_the_agent_all_2(self):
        self.folder = self.mkdir("all")
        _, ai = self.slash("/datastash:install")
        v = self.prompt("/datastash:install")[0]
        ctx = (v or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertIn('ledger_agent_register with name "all-2"', ctx)
        self.assertNotIn('name "all"', ctx)

    def test_the_ai_registering_a_pick_word_is_denied(self):
        for n in WORD_SPELLINGS:
            out = self.tool("PreToolUse", "ledger_agent_register", {"name": n})
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", repr(n))
            self.assertIn("so no agent is named like it", out["systemMessage"])
        out = self.tool("PreToolUse", "ledger_agent_register", {"name": "web-app-2"})
        self.assertNotEqual((out or {}).get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

    def test_fetch_link_and_api_register_refuse_a_pick_word(self):
        for word in WORDS + ("A-LL",):
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                ds.cmd_link([word, "--workbook", WB])
            self.assertFalse(os.path.exists(ds.conf_path(ds.slug(word))), word)
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                ds.cmd_fetch([word, "abcdef123456"])
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                ds.cmd_api(["POST", "/api/v1/agent/register", json.dumps({"name": word})])
        self.assertFalse([c for c in self.calls if c[1] in (ds.AGENT_REGISTER_PATH, ds.PICKUP_PATH)])

    # 3. an agent that already has such a name
    def test_cycle2_high_typing_all_with_an_agent_named_all_removes_nothing(self):
        """Stored-name HIGH: "all" alone never blocks; it lists every agent (the one named "all" too) and removes
        nothing until the person types "remove all 4"."""
        lst = plain("web-app", "alpha", "beta", "all", ids=[OWN, ALPHA, BETA, KEPT])
        out = self.picks("all", lst)
        self.assertEqual(out["systemMessage"], ds._plain_typed(ds.all_confirm_lines(ds.norm_list(lst))))
        self.assertIn("- all (public)", out["systemMessage"])
        self.assertTrue(self.denied())
        self.prompt("/datastash:uninstall")
        self.tool("PostToolUse", "ledger_agents_list", {}, lst)
        v, _ = self.prompt("all")
        self.assertEqual(v["decision"], "block")
        self.assertEqual(v["reason"], ds._plain_typed(ds.all_confirm_lines(ds.norm_list(lst))))
        self.assertTrue(self.denied())
        self.prompt("remove all 4")
        self.assertEqual(self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["updatedInput"]["targets"],
                         sorted([OWN, ALPHA, BETA, KEPT]))
        self.assertTrue(ds.pick_refused_line('"all"', ds.norm_list(lst), OWN).endswith(ds.OLD_STYLE_LINE))

    def test_cycle2_high_typing_this_with_an_agent_named_this_removes_nothing(self):
        lst = with_kept("this", "x")
        self.assertIsNone(picks_of("this", lst, OWN))
        self.kept_refusal(self.picks("this", lst)["systemMessage"], "this", "this")
        self.assertTrue(self.denied())

    def test_a_kept_name_is_never_picked_quoted_or_spelled_otherwise(self):
        for word in WORDS:
            lst = with_kept(word)
            for text in (word, f'"{word}"', word.upper(), f"“{word}”", f"web-app {word}", f"{word}, web-app",
                         f'"{word}" web-app', word[0] + "-" + word[1:]):
                if text.casefold() == "all":  # stored-name HIGH: "all" alone asks to confirm, never picks by name
                    self.assertEqual(picks_of(text, lst, OWN), "confirm")
                    continue
                self.assertIsNone(picks_of(text, lst, OWN), (word, text))
                self.assertTrue(ds.pick_refused_why(text, ds.norm_list(lst), OWN).startswith('"'), (word, text))

    def test_and_between_names_is_refused_while_an_agent_is_named_and(self):
        lst = with_kept("and", "x", "y")
        for text in ("x and y", "x, and, y", '"and"', "and"):
            self.assertIsNone(picks_of(text, lst), text)
        self.kept_refusal(ds.pick_refused_why("x and y", ds.norm_list(lst), None) + "", "and", "and")

    def test_existing_look_alike_spellings_are_kept_names_too(self):
        for name in ("ALL", "A-ll", "ａｌｌ", "\u0430ll", "a11", "Th1s", "a n d"):
            lst = with_kept(name, "x")
            word = ds.pick_word_like(name)
            self.assertEqual(picks_of(word, lst, OWN), "confirm" if word == "all" else None, name)
            self.assertIsNone(picks_of(f'"{name}"', lst, OWN), name)
            if " " not in name:  # unquoted, a name with spaces is several tokens, none of them a name
                self.assertEqual(picks_of(name, lst, OWN), "confirm" if name.casefold() == "all" else None, name)

    def test_other_names_still_work_beside_a_kept_name(self):
        lst = with_kept("all", "x", "y")
        self.assertEqual(sorted(picks_of("x y", lst)), sorted([X, Y]))
        self.assertEqual(picks_of("web-app", lst), [OWN])
        self.picks("x", lst)
        self.assertEqual(self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["updatedInput"]["targets"],
                         [X])

    def test_a_different_word_is_not_ambiguous(self):
        lst = with_kept("this", "x")
        self.assertEqual(picks_of("all", lst), "confirm")
        self.assertEqual(sorted(ds.all_confirm(ds.norm_list(lst))["ids"]), sorted([KEPT, OWN, X]))
        lst = with_kept("all", "x")
        self.assertEqual(picks_of("this", lst, OWN), [OWN])

    def test_visibility_never_acts_on_a_kept_name(self):
        lst = with_kept("all", "x")
        for text in ('"all"', "x all", "ALL x", '"a-ll"'):
            self.assertIsNone(ds.resolve_picks(text, ds.norm_list(lst), None, reserved=False), text)
        self.prompt('make "all" public')
        out = self.tool("PostToolUse", "ledger_agents_list", {}, lst)
        self.kept_refusal(out["systemMessage"], "all", "all")
        self.assertIn("Nothing was changed.", out["systemMessage"])
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], ds.VIS_REFUSED_AI)
        self.assertTrue(self.denied())
        self.assertIsNone(self.prompt("make all public")[0])  # "all" alone stays an ordinary prompt
        self.assertTrue(self.denied())

    def test_a_quoted_pick_word_with_no_such_agent_names_nothing(self):
        for word in WORDS:
            self.assertEqual(picks_of(f'"{word}"', AX_LIST), "none", word)
            self.assertIsNone(picks_of(f'"{word}" alex', AX_LIST), word)

    # 4. the attacks of review 1 and review 2, again
    def test_every_earlier_attack_again(self):
        lst = ds.norm_list(plain("a", "b", "c", "bc", "alpha", "beta", "alpha-beta", "alex", "alex-2", "my agent",
                                 ids=[str(uuid.UUID(int=0x100 + i)) for i in range(10)]))
        ids = {a["name"]: a["id"] for a in lst["agents"]}
        every = sorted(ids.values())
        want = {
            "a b c": ["a", "b", "c"], "bc": ["bc"], "b c": ["b", "c"], "alpha beta": ["alpha", "beta"],
            "alpha-beta": ["alpha-beta"], "alex alex-2": ["alex", "alex-2"], "alex-2, alex": ["alex", "alex-2"],
            "alex and alex-2": ["alex", "alex-2"], "alex alex ALEX": ["alex"],
            '"alpha" alpha': ["alpha"], "all": "every", "  ALL  ": "every", "all,": None,
        }
        for text, exp in want.items():
            got = ds.resolve_picks(text, lst, None)
            if exp is None:  # confirm review HIGH (PM ruling): "all" with a stray separator refuses the command
                self.assertIsNone(got, text)
                continue
            if exp == "every":  # stored-name HIGH: "all" alone asks to confirm every agent first
                self.assertEqual(got, "confirm", text)
                self.assertEqual(sorted(ds.all_confirm(lst)["ids"]), every)
                continue
            self.assertEqual(sorted(got), sorted(ids[n] for n in exp), text)
        refused = ("x all", "all x", "alex all", "all alex", "alpha beta all", "all, alpha", "x,all", "all all",
                   "this x", "a ll", "alex a ll", "alex nobody", 'alex "alex-2', '"my agent', 'my agent"', '"x"y',
                   '"my agent" x"', "alex \u201calex-2\"", "alpha.", "alpha!", "(alpha)", "alpha/beta", "alex 2",
                   "alex alex2", "a1ex", 'alex ""', "alex, " + "x" * 4000, '"My Agent" a', "my agent")
        for text in refused:
            self.assertNotIsInstance(ds.resolve_picks(text, lst, None), list, text)
        for text in ("make alex all public", "make alex, all public", "make alex this private"):
            got = ds.VISIBILITY_RE.fullmatch(text)
            self.assertNotIsInstance(ds.resolve_picks(got.group(1), lst, None, reserved=False), list, text)
        self.assertEqual(sorted(ds.resolve_picks("a b c", lst, None, reserved=False)), sorted([ids["a"], ids["b"], ids["c"]]))
        self.assertIsNone(ds.resolve_picks("al\u200bpha", lst, None))  # 1.0.11 final: zero-width is not ASCII: refused
        for text in ("\u0430lpha", "ａｌｐｈａ", "alpha\u0301"):  # look-alikes are unknown
            self.assertNotIsInstance(ds.resolve_picks(text, lst, None), list, repr(text))
        self.assertEqual(ds.resolve_picks("tell me about alpha", lst, None), None)
        many = " ".join(["alex"] * 101)
        self.assertIsNone(ds.resolve_picks(many, lst, None))
        self.assertEqual(ds.resolve_picks(" ".join(["alex"] * 100), lst, None), [ids["alex"]])
        t0 = time.time()
        for text in ("x" * 4000, '"' * 4000, '"a' * 2000, " ".join(["word"] * 1999)):
            ds.resolve_picks(text, lst, None)
        self.assertLess(time.time() - t0, 2)

    # 5. the three LOWs of review 2
    def test_low1_a_token_longer_than_64_never_matches_by_its_first_64(self):
        long = "n" * 64
        lst = plain(long, "x", ids=[A, B])
        self.assertEqual(picks_of(long, lst), [A])
        self.assertNotIsInstance(picks_of(long + "zzz", lst), list)
        self.assertIsNone(picks_of(long + "zzz x", lst))
        self.assertTrue(ds.pick_refused_why(long + "zzz x", ds.norm_list(lst), None).startswith(f'"{long}..." is not on the list'))

    def test_low2_a_long_ordinary_prompt_after_the_list_passes_through(self):
        self.prompt("/datastash:uninstall")
        self.tool("PostToolUse", "ledger_agents_list", {}, AX_LIST)
        self.assertIsNone(self.prompt(" ".join(["word"] * 150))[0])
        self.assertTrue(self.denied())

    def test_low2_more_than_100_names_is_still_refused(self):
        self.assertIsNone(picks_of(" ".join(["alex"] * 101), AX_LIST))
        self.assertEqual(ds.pick_refused_why(" ".join(["alex"] * 101), ds.norm_list(AX_LIST), None),
                         "more than 100 names were typed")

    def test_low3_a_quoted_exact_name_picks_one_of_two_near_names(self):
        lst = plain("alpha", "alpha.", "Alex", "alex", ids=[A, B, C, BC])
        self.assertIsNone(picks_of("alpha", lst))  # unquoted stays refused
        self.assertIsNone(picks_of("alpha.", lst))
        self.assertEqual(picks_of('"alpha"', lst), [A])
        self.assertEqual(picks_of('"alpha."', lst), [B])
        self.assertEqual(picks_of('"alex"', lst), [BC])
        self.assertEqual(picks_of('"Alex"', lst), [C])
        self.assertIsNone(picks_of('"ALEX"', lst))  # not character for character either of them
        self.assertIn("type one in double quotes exactly as the list shows it", ds.pick_refused_why("alpha", ds.norm_list(lst), None))
        out = self.picks('"alpha."', lst)
        self.assertEqual(self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["updatedInput"]["targets"], [B])
        self.assertIsNotNone(out)


LOOKALIKES = ("\u13aall", "\u1d00\u029f\u029f", "\uab7all", "\u1d1bhis", "\u1d1b\u029c\u026a\ua731", "\u13a2his",
              "\u0430ll", "\u0430nd", "th\u0456s", "\uff41\uff4c\uff4c", "\uff54\uff48\uff49\uff53", "a\u200bll",
              "al\u200dl", "\u2060this", "all\u3164", "a\u01c1", "all\u0301")  # Ꭺll ᴀʟʟ ꭺll ᴛhis ᴛʜɪꜱ Ꭲhis, Cyrillic, fullwidth, zero-width


class PlainNames(Base):
    """1.0.11 final (PM ruling after the redesign review HIGH): agent names are plain ASCII (a-z, 0-9, "-", "_", "."),
    everywhere. Every naming path makes a folder name plain; typed names are ASCII tokens only; an existing agent whose
    stored name is not plain ASCII is never picked by name, and while one that may read as "all", "this" or "and" is
    listed those words are refused too."""
    def setUp(self):
        super().setUp()
        self.installed()

    def mkdir(self, name):
        d = os.path.join(os.path.dirname(self.folder), name)
        os.makedirs(d, exist_ok=True)
        return d

    def picks(self, args, lst):
        self.slash("/datastash:uninstall " + args)
        return self.tool("PostToolUse", "ledger_agents_list", {}, lst)

    def denied(self):
        return self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["permissionDecision"] == "deny"

    # 1. the rule that makes a folder name plain
    def test_the_plain_name_rule(self):
        want = {"\u13aall": "ll", "\u1d00\u029f\u029f": "", "\u1d1bhis": "his", "My Project": "my-project",
                "  --a__b--  ": "a__b", "Q4  launch--plan": "q4-launch-plan", "caf\u00e9 bot": "caf-bot", "\u0430ll": "ll",
                "\uff41\uff4c\uff4c": "", "a\u200bll": "a-ll", "web.App_2": "web.app_2", "\u212aey": "ey",
                "\u0130stanbul": "stanbul", "x" * 50: "x" * 40, "-" * 5: "", "...": "", "_": ""}
        for raw, plain_ in want.items():
            self.assertEqual(ds.plain_agent_name(raw), plain_, repr(raw))
            self.assertTrue(plain_ == "" or ds.plain_name(plain_), repr(raw))

    def test_every_folder_and_session_name_becomes_plain(self):
        for raw in LOOKALIKES + ("My Project", "caf\u00e9", "\u65e5\u672c", "UPPER Case"):
            name = ds._free_name(self.mkdir(raw))
            self.assertTrue(ds.plain_name(name), (raw, name))
            self.assertEqual(name, name.lower(), raw)
            self.assertIsNone(ds.pick_word_like(name), (raw, name))
            kind, auto = ds.auto_name(self.mkdir("s-" + raw.replace("/", "-")), None, session_name=ds._session_name({"agent_type": raw}))
            self.assertTrue(ds.plain_name(auto) and ds.pick_word_like(auto) is None, (raw, auto))
        self.assertEqual(ds._free_name(self.mkdir("\u13aall")), "ll")  # the ruling's example
        with mock.patch("getpass.getuser", return_value="pat"):  # 1.0.16: nothing left: the computer's user, never "agent"
            self.assertEqual(ds._free_name(self.mkdir("\u1d00\u029f\u029f")), "pat")
        self.assertEqual(ds._free_name(self.mkdir("a\u200bll")), "a-ll-2")  # reads as "all": the -2 rule
        self.assertEqual(ds._free_name(self.mkdir("ALL")), "all-2")
        self.assertEqual(ds.auto_name(self.mkdir("q"), None, after="My Agent")[1], "my-agent-2")

    def test_the_install_command_registers_the_plain_name(self):
        self.folder = self.mkdir("\u13a2his Project")
        self.slash("/datastash:install")
        ctx = ((self.prompt("/datastash:install")[0] or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")
        self.assertIn('ledger_agent_register with name "his-project"', ctx)

    # 2. names sent for a new agent
    def test_the_ai_registering_a_name_that_is_not_plain_is_denied(self):
        for n in LOOKALIKES + ("Research Bot", "caf\u00e9", "web/app", "a,b", "x\ty"):
            out = self.tool("PreToolUse", "ledger_agent_register", {"name": n})
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", repr(n))
        for n in ("research-bot", "web.app_2", "Researcher"):
            out = self.tool("PreToolUse", "ledger_agent_register", {"name": n})
            self.assertNotEqual((out or {}).get("hookSpecificOutput", {}).get("permissionDecision"), "deny", n)

    def test_an_agent_already_here_keeps_its_name(self):
        """Redesign LOW 2: this computer's agent made before names were plain (or named "and") finishes install."""
        for n in ("and", "My Agent"):
            ds.save_agent(n, str(uuid.uuid4()), KEY, WB, self.mkdir("old-" + n.replace(" ", "")))
            out = self.tool("PreToolUse", "ledger_agent_register", {"name": n})
            self.assertNotEqual((out or {}).get("hookSpecificOutput", {}).get("permissionDecision"), "deny", n)
        out = self.tool("PreToolUse", "ledger_agent_register", {"name": "Your Agent"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_api_register_and_join_redeem_refuse_names_that_are_not_plain(self):
        """Redesign LOW 3: the join path is checked too."""
        for path in (ds.AGENT_REGISTER_PATH, "/api/v1/agent/join/redeem", "/api/v1/agent/join/redeem/"):
            for n in LOOKALIKES[:6] + ("and", "Research Bot"):
                with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                    ds.cmd_api(["POST", path, json.dumps({"name": n, "code": "x"})])
        self.assertFalse([c for c in self.calls if "register" in c[1] or "redeem" in c[1]])

    def test_link_and_fetch_refuse_a_name_that_is_not_ascii(self):
        for n in LOOKALIKES:
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                ds.cmd_link([n, "--workbook", WB])
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                ds.cmd_fetch([n, "abcdef123456"])
        self.assertFalse([c for c in self.calls if c[1] == ds.PICKUP_PATH])

    # 3. typed names
    def test_a_typed_name_that_is_not_ascii_refuses_the_whole_command(self):
        lst = plain("web-app", "alpha", "all-2", ids=[OWN, X, Y])
        for tok in LOOKALIKES + ("alph\u0430", "\uff41lpha", "alpha\u200b"):
            for text in (tok, f"alpha {tok}", f'"{tok}"', f"{tok}, web-app"):
                self.assertIsNone(picks_of(text, lst, OWN), repr(text))
                self.assertIsNone(ds.resolve_picks(text, ds.norm_list(lst), None, reserved=False), repr(text))
                self.assertIn("not plain ASCII", ds.pick_refused_why(text, ds.norm_list(lst), OWN), repr(text))
        out = self.picks("\u13aall", lst)
        self.assertIn("Nothing was removed.", out["systemMessage"])
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], ds.PICK_REFUSED_AI)
        self.assertTrue(self.denied())
        self.prompt("/datastash:uninstall")
        self.tool("PostToolUse", "ledger_agents_list", {}, lst)
        v, _ = self.prompt("\u1d00\u029f\u029f")
        self.assertEqual(v["decision"], "block")
        self.assertIn("Nothing was removed.", v["reason"])
        self.assertTrue(self.denied())
        self.prompt("make \u1d1bhis public")
        out = self.tool("PostToolUse", "ledger_agents_list", {}, lst)
        self.assertIn("Nothing was changed.", out["systemMessage"])
        self.assertTrue(self.denied())

    # 4. an agent whose stored name is not plain ASCII
    def test_redesign_high_typing_all_or_this_beside_a_look_alike_agent_removes_nothing(self):
        for odd in LOOKALIKES:
            lst = plain("web-app", "vault", odd, ids=[OWN, X, KEPT])
            words = ds._old_words(ds.show_name(odd))  # what the name as listed may read as
            for word in ("this", "and"):  # refused only while the listed name may read as that very word
                if word in words:
                    self.assertIsNone(picks_of(word, lst, OWN), (odd, word))
                    line = ds.pick_refused_line(word, ds.norm_list(lst), OWN)
                    self.assertIn("old-style name", line, repr(odd))
                    self.assertTrue(line.endswith(ds.OLD_STYLE_LINE), line)
                    self.assertNotIn("agents page", line)
            self.assertNotIsInstance(picks_of('"all"', lst, OWN), list, repr(odd))
            self.assertEqual(picks_of("vault and web-app", lst, OWN), None if "and" in words else [X, OWN], repr(odd))
            self.assertEqual(picks_of("this", lst, OWN), None if "this" in words else [OWN], repr(odd))
            for word in ("all", "ALL"):  # stored-name HIGH: "all" alone never blocks; it asks to confirm
                self.assertEqual(picks_of(word, lst, OWN), "confirm", (odd, word))
            self.assertEqual(sorted(picks_of("vault web-app", lst, OWN)), sorted([X, OWN]), repr(odd))
            out = self.picks("all", lst)
            self.assertEqual(out["systemMessage"], ds._plain_typed(ds.all_confirm_lines(ds.norm_list(lst))), repr(odd))
            self.assertTrue(self.denied())
            if "this" in words:
                self.prompt("/datastash:uninstall")
                self.tool("PostToolUse", "ledger_agents_list", {}, lst)
                v, _ = self.prompt("this")
                self.assertEqual(v["decision"], "block", repr(odd))
                self.assertTrue(self.denied())

    def test_an_agent_whose_name_is_not_plain_ascii_is_never_picked_by_name(self):
        for odd, typed in (("alpha\u202e", "alpha"), ("caf\u00e9", "cafe"), ("\u0430lpha", "alpha"),
                           ("\uff41\uff4c\uff50\uff48\uff41", "alpha"), ("al\u200bpha", "alpha"),
                           ("\u1d21eb", "web")):
            lst = plain("web-app", odd, ids=[OWN, KEPT])
            for text in (typed, f'"{typed}"', typed.upper(), f"web-app {typed}"):
                self.assertIsNone(picks_of(text, lst, OWN), (odd, text))
            line = ds.pick_refused_line(typed, ds.norm_list(lst), OWN)
            self.assertIn("has an old-style name", line, repr(odd))
            self.assertTrue(line.endswith(ds.OLD_STYLE_LINE), line)
            self.assertEqual(picks_of("web-app", lst, OWN), [OWN], repr(odd))
        lst = plain("web-app", "alpha\u202e", "beta", ids=[OWN, KEPT, X])
        self.assertEqual(picks_of("all", lst, OWN), "confirm")
        lst = plain("web-app", "\u1d00\u029f\u029f", "vault", ids=[OWN, KEPT, X])  # "???": any 3 letters may be it
        self.assertIsNone(picks_of("web", plain("web", "\u1d00\u029f\u029f", ids=[OWN, KEPT]), OWN))
        self.assertEqual(picks_of("vault", lst, OWN), [X])

    def test_the_stored_name_decides_not_the_shown_one(self):
        got = ds.norm_list(plain("al\u200bpha", "alpha\u202e", "alpha", ids=[A, B, C]))
        self.assertEqual([a["name"] for a in got["agents"]], ["alpha", "alpha", "alpha"])  # shown alike
        self.assertEqual([a["plain"] for a in got["agents"]], [False, False, True])
        self.assertIsNone(ds.resolve_picks("alpha", got, None))
        self.assertIsNone(ds.resolve_picks('"alpha"', got, None))

    def test_the_plugin_picks_agents_only_by_name_never_by_id(self):
        lst = plain("web-app", "alpha", ids=[OWN, X])
        self.assertEqual(picks_of(X, lst, OWN), "none")  # an agent id typed is not on the list
        self.assertIsNone(picks_of(f"alpha {X}", lst, OWN))

    def test_every_attack_of_the_three_reviews_with_a_look_alike_listed(self):
        lst = ds.norm_list(plain("a", "b", "c", "bc", "alpha", "beta", "alpha-beta", "\u13aall",
                                 ids=[str(uuid.UUID(int=0x200 + i)) for i in range(8)]))
        ids = {a["name"]: a["id"] for a in lst["agents"]}
        for text, exp in {"a b c": ["a", "b", "c"], "bc": ["bc"], "alpha beta": ["alpha", "beta"],
                          "alpha-beta": ["alpha-beta"], "alpha, beta": ["alpha", "beta"]}.items():
            self.assertEqual(sorted(ds.resolve_picks(text, lst, None)), sorted(ids[n] for n in exp), text)
        for text in ("all", "this", "x all", "all x", "alpha all", "all, alpha", "alpha and beta", "a ll", '"all"',
                     "\u13aall", "alpha \u13aall", "make alpha all public", "alpha\u200b", "remove all"):
            self.assertNotIsInstance(ds.resolve_picks(text, lst, None), list, repr(text))
            self.assertNotIsInstance(ds.resolve_picks(text, lst, None, reserved=False), list, repr(text))


# 1.0.11 final review CRITICAL: names prod allows today that hold a separator next to a pick word, and other old-style ones
OLD_WORDLIKE = ("all,", ",all", "all ,", "all and", "and all", "this,", "ALL,", '"all"')
OLD_PLAIN = ("a b", "x;y", "my agent", "web/app", "alpha!", "(beta)")


class OldStyleNames(Base):
    """1.0.11 final (PM ruling after the final review CRITICAL): a STORED agent name is picked by name only when the whole
    name matches ^[a-z0-9._-]+$ (ignoring case) and is not a pick word. Any other stored name ("all,", "all and", "this,",
    "a b", "x;y", a look-alike) is an old-style name, never picked by name: the command is refused with a plain line
    (it has an old-style name and can't be removed by name; "all" still removes it, unless it may read as a pick word).
    Typed tokens must match the same rule."""
    def setUp(self):
        super().setUp()
        self.installed()

    def picks(self, args, lst):
        self.slash("/datastash:uninstall " + args)
        return self.tool("PostToolUse", "ledger_agents_list", {}, lst)

    def denied(self):
        return self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["permissionDecision"] == "deny"

    def targets(self):
        return self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["updatedInput"]["targets"]

    def test_the_stored_name_is_plain_only_by_the_strict_rule(self):
        got = ds.norm_list(plain(*(OLD_WORDLIKE + OLD_PLAIN + ("alex", "alex-2", "Web.App_2", "Ꭺll"))))
        self.assertEqual([a["plain"] for a in got["agents"]],
                         [False] * (len(OLD_WORDLIKE) + len(OLD_PLAIN)) + [True, True, True, False])

    def test_typing_an_old_name_as_listed_removes_nothing(self):
        for old in OLD_WORDLIKE + OLD_PLAIN:
            lst = plain("web-app", "vault", old, ids=[OWN, X, KEPT])
            for text in (old, old.upper(), f'"{old}"', f"vault {old}", f"{old} vault", f"  {old}  "):
                got = picks_of(text, lst, OWN)
                # only "all" alone asks to confirm the whole list; "all," and "all and" refuse (confirm review HIGH)
                alone = text.strip().casefold() == "all"
                self.assertEqual(got, "confirm" if alone else None, (old, text))
                self.assertIsNone(ds.resolve_picks(text, ds.norm_list(lst), None, reserved=False), (old, text))
            line = ds.pick_refused_line(f"vault {old}", ds.norm_list(lst), OWN)
            self.assertEqual(line, f'Not done: your agent "{old}" can\'t be picked by name. Nothing was removed. '
                             + ds.OLD_STYLE_LINE, old)
            self.assertEqual(ds.OLD_STYLE_LINE, "That agent has an old-style name. To remove it, use "
                             "**/datastash:uninstall all** and confirm.")
            self.assertTrue(ds.vis_refused_line(f"vault {old}", ds.norm_list(lst)).endswith(
                "Nothing was changed. " + ds.OLD_STYLE_LINE))
            self.assertEqual(sorted(picks_of("vault web-app", lst, OWN)), sorted([X, OWN]), old)

    def test_the_critical_end_to_end_all_comma_on_the_slash_line_and_after_the_list(self):
        for old in ("all,", "all and", ",all", "and all", "all ,"):
            lst = plain("web-app", "vault", old, ids=[OWN, X, KEPT])
            alone = old.strip().casefold() == "all"  # confirm review HIGH: "all,", ",all", "all ," refuse, never confirm
            out = self.picks(old, lst)
            if alone:  # stored-name HIGH: the confirm step, which lists the old-style name too; nothing removed yet
                self.assertEqual(out["systemMessage"], ds.all_confirm_lines(ds.norm_list(lst)), old)
                self.assertIn(f"- {old} (public)", out["systemMessage"])
            else:
                self.assertEqual(out["systemMessage"], f'Not done: your agent "{old}" can\'t be picked by name. Nothing '
                                 "was removed. " + ds._plain_typed(ds.OLD_STYLE_LINE), old)
                self.assertEqual(out["hookSpecificOutput"]["additionalContext"], ds.PICK_REFUSED_AI)
            self.assertTrue(self.denied(), old)
            self.prompt("/datastash:uninstall")
            self.tool("PostToolUse", "ledger_agents_list", {}, lst)
            v, _ = self.prompt(old)
            self.assertEqual(v["decision"], "block", old)
            self.assertIn("This removes all 3" if alone else ds._plain_typed(ds.OLD_STYLE_LINE), v["reason"])
            self.assertTrue(self.denied(), old)

    def test_this_comma_never_removes_this_sessions_agent(self):
        lst = plain("web-app", "x", "this,", ids=[OWN, X, KEPT])
        for text in ("this,", "this", "THIS ,", '"this,"'):
            self.assertIsNone(picks_of(text, lst, OWN), text)
        out = self.picks("this,", lst)
        self.assertIn("Nothing was removed.", out["systemMessage"])
        self.assertTrue(self.denied())

    def test_an_old_name_that_may_read_as_a_pick_word_refuses_the_words_too(self):
        """Stored-name HIGH: "all" is never refused for an old-style name (alone it asks to confirm); "this" and "and"
        are refused only while an old-style name may read as that very word (LOW 3: "all," no longer blocks "and")."""
        for old in OLD_WORDLIKE + ("Ꭺll", "Ꭺll,", "a n d", "this?", "t,his", "(and)"):
            lst = plain("web-app", "vault", old, ids=[OWN, X, KEPT])
            words = ds._old_words(old)
            for word in ("all", "ALL"):
                self.assertEqual(picks_of(word, lst, OWN), "confirm", (old, word))
            self.assertIsNone(picks_of("all,", lst, OWN), old)  # confirm review HIGH: a stray separator refuses
            self.assertEqual(picks_of("this", lst, OWN), None if "this" in words else [OWN], old)
            self.assertEqual(picks_of("vault and web-app", lst, OWN), None if "and" in words else [X, OWN], old)
            for word in ("this", "and"):
                if word in words:
                    line = ds.pick_refused_line(word, ds.norm_list(lst), OWN)
                    self.assertIn("old-style name", line, old)
                    self.assertTrue(line.endswith(ds.OLD_STYLE_LINE), line)
        self.assertEqual(ds._old_words("all,"), {"all"})
        self.assertEqual(ds._old_words("this,"), {"this"})
        self.assertEqual(ds._old_words("all and"), {"all", "and"})

    def test_all_alone_still_removes_an_old_name_that_reads_as_no_word(self):
        """Stored-name HIGH: "all" alone asks to confirm every agent, old-style ones too, and the refusal says so."""
        for old in OLD_PLAIN + ("alpha‮", "café-long-name"):
            lst = plain("web-app", "vault", old, ids=[OWN, X, KEPT])
            self.assertEqual(picks_of("all", lst, OWN), "confirm", old)
            if old.isascii():
                self.assertTrue(ds.pick_refused_line(old, ds.norm_list(lst), OWN).endswith(ds.OLD_STYLE_LINE), old)
        out = self.picks("all", plain("web-app", "a b", ids=[OWN, KEPT]))
        self.assertIn("- a b (public)", out["systemMessage"])
        self.prompt("remove all 2")
        self.assertEqual(sorted(self.targets()), sorted([OWN, KEPT]))

    def test_an_old_name_never_blocks_names_beside_it(self):
        lst = plain("a", "b", "a b", "x", "y", "x;y", "alpha", "beta", ids=[str(uuid.UUID(int=0x300 + i)) for i in range(8)])
        ids = {a["name"]: a["id"] for a in ds.norm_list(lst)["agents"]}
        for text, exp in {"a, b": ["a", "b"], "a": ["a"], "x y": ["x", "y"], "alpha beta": ["alpha", "beta"],
                          "x, y": ["x", "y"]}.items():
            self.assertEqual(sorted(picks_of(text, lst)), sorted(ids[n] for n in exp), text)
        for text in ("a b", "ab", "x;y", '"a b"', "alpha a b"):
            self.assertIsNone(picks_of(text, lst), text)
        self.assertNotIsInstance(picks_of("xy", lst), list)

    def test_a_typed_token_must_match_the_strict_rule(self):
        lst = plain("web-app", "alpha", ids=[OWN, X])
        for tok in ("alpha!", "(alpha)", "alpha/beta", "x;y", '"my agent"', "alpha?"):
            self.assertNotIsInstance(picks_of(tok, lst, OWN), list, tok)
            self.assertIsNone(picks_of(f"alpha {tok}", lst, OWN), tok)
        self.assertIn("is not a plain name", ds.pick_refused_why("alpha x;y", ds.norm_list(lst), OWN))

    def test_a_stored_look_alike(self):
        for odd in LOOKALIKES:
            lst = plain("web-app", "vault", odd, ids=[OWN, X, KEPT])
            self.assertEqual(picks_of("all", lst, OWN), "confirm", repr(odd))
            words = ds._old_words(ds.show_name(odd))
            for text in ("this", "and", odd, f'"{odd}"', "vault and web-app"):
                w = "this" if text == "this" else "and" if "and" in text.split() else None
                if w is not None and w not in words:  # stored-name HIGH: only the word the listed name may read as
                    self.assertNotIn(KEPT, picks_of(text, lst, OWN) or [], (odd, text))
                    continue
                if text.strip('"').casefold() == "all" and not text.startswith('"'):
                    continue
                self.assertNotIsInstance(picks_of(text, lst, OWN), list, (odd, text))
            self.assertEqual(sorted(picks_of("vault web-app", lst, OWN)), sorted([X, OWN]), repr(odd))

    def test_the_owners_case_alex_and_alex_2(self):
        for extra in ((), ("all,",), ("a b", "x;y"), ("this,", "Ꭺll")):
            lst = plain("web-app", "alex", "alex-2", *extra, ids=[OWN, AX, AX2] + [KEPT, X, Y][:len(extra)])
            for text, exp in {"alex alex-2": [AX, AX2], "alex-2 alex": [AX, AX2], "alex, alex-2": [AX, AX2],
                              "alex and alex-2": [AX, AX2], "ALEX  Alex-2": [AX, AX2], "alex": [AX], "alex-2": [AX2],
                              '"alex"': [AX], '"alex-2"': [AX2]}.items():
                if extra and "and" in text.split() and any("and" in ds._old_words(n) for n in extra):
                    self.assertIsNone(picks_of(text, lst, OWN), (extra, text))  # "and" is refused while one is listed
                    continue
                self.assertEqual(sorted(picks_of(text, lst, OWN)), sorted(exp), (extra, text))
        lst = plain("web-app", "alex", "alex-2", "all,", ids=[OWN, AX, AX2, KEPT])
        self.picks("alex alex-2", lst)
        self.assertEqual(sorted(self.targets()), sorted([AX, AX2]))
        self.picks("alex", lst)
        self.assertEqual(self.targets(), [AX])
        self.picks("alex-2", lst)
        self.assertEqual(self.targets(), [AX2])

    def test_every_attack_of_every_review_with_old_names_listed(self):
        lst = ds.norm_list(plain("a", "b", "c", "bc", "alpha", "beta", "alpha-beta", "alex", "alex-2", "all,", "a b",
                                 "this,", "x;y", "Ꭺll", "all", ids=[str(uuid.UUID(int=0x400 + i)) for i in range(15)]))
        ids = {a["name"]: a["id"] for a in lst["agents"]}
        for text, exp in {"a, b, c": ["a", "b", "c"], "bc": ["bc"], "b c": ["b", "c"], "alpha beta": ["alpha", "beta"],
                          "alpha-beta": ["alpha-beta"], "alpha, beta": ["alpha", "beta"], "alex alex-2": ["alex", "alex-2"],
                          "alex-2, alex": ["alex", "alex-2"], "alex alex ALEX": ["alex"], '"alpha" alpha': ["alpha"]}.items():
            self.assertEqual(sorted(ds.resolve_picks(text, lst, None)), sorted(ids[n] for n in exp), text)
        for text in ("a b c",  # holds the old-style "a b" as listed: refused whole (fail closed)
                     "all", "this", "and", "all,", ",all", "all and", "this,", "x;y", '"all,"', '"a b"', "a b",
                     "x all", "all x", "alpha all", "all, alpha", "x,all", "all all", "this x", "alpha and beta", "a ll",
                     '"all"', "Ꭺll", "alpha Ꭺll", "alpha​", "remove all", "alex nobody", 'alex "alex-2',
                     "alpha.", "alpha!", "(alpha)", "alpha/beta", "alex 2", "alex alex2", "a1ex", "аlpha",
                     "alex, " + "x" * 4000, " ".join(["alex"] * 101), "n" * 65):
            self.assertNotIsInstance(ds.resolve_picks(text, lst, None), list, repr(text[:40]))
            self.assertNotIsInstance(ds.resolve_picks(text, lst, None, reserved=False), list, repr(text[:40]))
        for text in ("make alex all public", "make alex, all public", "make alex this private", "make all, public",
                     "make this, private"):
            got = ds.VISIBILITY_RE.fullmatch(text)
            self.assertNotIsInstance(ds.resolve_picks(got.group(1), lst, None, reserved=False), list, text)

    def test_low1_a_folder_named_only_with_dots_or_underscores_is_agent(self):
        for raw in ("...", ".", "_", "._-", "__"):
            self.assertEqual(ds.plain_agent_name(raw), "", raw)
            d = os.path.join(os.path.dirname(self.folder), "f" + str(len(raw)), raw)
            os.makedirs(d, exist_ok=True)
            self.assertEqual(ds._folder_name(d), "", raw)  # 1.0.16: never "agent"
            with mock.patch("getpass.getuser", return_value="pat"):
                self.assertEqual(ds._free_name(d), "pat", raw)  # the computer's user instead
                self.assertEqual(ds.auto_name(d, None), ("name", "pat"))
            with mock.patch("getpass.getuser", return_value="..."), mock.patch.object(ds, "HOME", "/"):
                self.assertEqual(ds.auto_name(d, None), ("ask", None))  # nothing at all: the person is asked


class AllConfirm(Base):
    """1.0.11 stored-name HIGH (PM ruling): "all" alone never blocks. It lists every agent by its shown name, old-style
    names too, and asks the person to type exactly "remove all N" (N = the count shown). Only that reply, typed by the
    person (the same typed proof as any pick), removes them; any other reply removes nothing and closes the step."""
    OLD_LIST = plain("web-app", "alex", "alex-2", "all,", "a b", "\u13aall", "all and", "x;y",
                     ids=[OWN, AX, AX2] + [str(uuid.UUID(int=0x500 + i)) for i in range(5)])

    def setUp(self):
        super().setUp()
        self.installed()

    def open_step(self, lst=LIST, slash=False):
        if slash:
            self.slash("/datastash:uninstall all")
            return self.tool("PostToolUse", "ledger_agents_list", {}, lst)["systemMessage"]
        self.prompt("/datastash:uninstall")
        self.tool("PostToolUse", "ledger_agents_list", {}, lst)
        v, _ = self.prompt("all")
        self.assertEqual(v["decision"], "block")  # the reply goes to the person only, never to the model
        return v["reason"]

    def note(self):
        return self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]

    def test_the_exact_count_removes_every_agent_with_the_persons_typed_proof(self):
        for slash in (False, True):
            shown = self.open_step(slash=slash)
            self.assertEqual(shown, "This removes all 3 of your datastash agents:\n- web-app (public)\n- alpha (private)\n"
                             "- beta (public)\nTo go ahead, type exactly: \"remove all 3\". Anything else removes nothing.")
            self.assertEqual(self.note()["permissionDecision"], "deny")  # nothing is picked before the confirm
            v, pid = self.prompt("remove all 3")
            self.assertIn('kind "uninstall"', v["hookSpecificOutput"]["additionalContext"])
            got = self.note()["updatedInput"]
            self.assertEqual(got["targets"], sorted([OWN, ALPHA, BETA]))
            self.assertTrue(proof_ok(got["typed_proof"], "uninstall", [OWN, ALPHA, BETA], pid, "remove all 3"))
            self.assertEqual(self.note()["permissionDecision"], "deny")  # one use
        self.open_step()
        self.prompt("  Remove  ALL 3 ")  # case and runs of spaces do not matter; the count does
        self.assertEqual(self.note()["updatedInput"]["targets"], sorted([OWN, ALPHA, BETA]))

    def test_a_wrong_count_or_any_other_reply_removes_nothing(self):
        for reply in ("remove all 2", "remove all 4", "remove all", "remove all three", "yes", "all", "remove all 3 now",
                      "remove all 03", "alpha", "remove all 3.", "\"remove all 3\"", "remove all ３", "this"):
            self.open_step()
            v, _ = self.prompt(reply)
            self.assertEqual(v["decision"], "block", reply)
            self.assertEqual(v["reason"], ds.ALL_NOT_CONFIRMED_LINE, reply)
            self.assertIn("Nothing was removed.", v["reason"])
            self.assertEqual(self.note()["permissionDecision"], "deny", reply)
            self.assertIsNone(self.prompt("remove all 3")[0], reply)  # the step is closed: an ordinary prompt now
            self.assertEqual(self.note()["permissionDecision"], "deny", reply)
        self.assertTrue(os.path.exists(ds.key_path("web-app")))

    def test_a_confirm_not_typed_by_the_person_is_refused(self):
        self.open_step()
        self.prompt("remove all 3", source="sdk")  # a headless or model-made prompt: no person origin
        out = self.tool("PreToolUse", "ledger_agents_note", {})
        self.assertEqual(out["systemMessage"], ds.NOT_PERSON_LINE)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.open_step()
        self.prompt("remove all 3", entry="remove all 2")  # the transcript does not hold the person's exact reply
        self.assertEqual(self.tool("PreToolUse", "ledger_agents_note", {})["systemMessage"], ds.NOT_TYPED_LINE)
        self.open_step()  # the model calling the note itself while the step is open
        self.assertEqual(self.tool("PreToolUse", "ledger_agents_note", {"targets": [OWN, ALPHA, BETA]})["systemMessage"],
                         ds.NO_STEP_LINE)
        self.assertTrue(os.path.exists(ds.key_path("web-app")))

    def test_the_confirm_is_bound_to_the_list_shown(self):
        self.open_step()
        other = dict(LIST, list_id=str(uuid.UUID(int=0xA2)))
        # 1.0.16 (real e2e): a list fetched again with exactly the same agents keeps the step open on that newest list
        again = self.tool("PostToolUse", "ledger_agents_list", {}, other)
        self.assertTrue(again["systemMessage"].startswith("This removes all 3 of your datastash agents:"))
        v, pid = self.prompt("remove all 3")
        self.assertNotIn("reason", v)
        got = self.note()["updatedInput"]
        self.assertEqual(got["list_id"], other["list_id"])
        self.open_step()
        self.tool("PostToolUse", "ledger_agents_list", {}, dict(LIST, agents=LIST["agents"][:2]))  # other agents
        self.assertEqual(self.prompt("remove all 2")[0]["reason"], ds.ALL_NOT_CONFIRMED_LINE)
        self.assertEqual(self.note()["permissionDecision"], "deny")

    def test_old_style_names_are_listed_and_removed_by_the_confirmed_all(self):
        lst = ds.norm_list(self.OLD_LIST)
        for slash in (False, True):
            shown = self.open_step(self.OLD_LIST, slash=slash)
            self.assertTrue(shown.startswith("This removes all 8 of your datastash agents:"), shown)
            for a in lst["agents"]:
                self.assertIn(f"\n- {a['name']} (public)", shown)
            self.assertNotIn("agents page", shown)
            self.prompt("remove all 8")
            self.assertEqual(self.note()["updatedInput"]["targets"], sorted(a["id"] for a in lst["agents"]))
        for old in ("all,", "all and", "x;y", "a b"):  # by name they stay refused, with the one way out
            line = ds.pick_refused_line(f"alex {old}", lst, OWN)
            self.assertTrue(line.endswith(ds.OLD_STYLE_LINE), line)
            self.assertNotIn("agents page", line)
            self.assertNotIn("/agents", line.replace("/datastash:uninstall", ""))

    def test_alex_and_alex_2_together_and_alone_beside_old_style_names(self):
        for text, want in (("alex alex-2", [AX, AX2]), ("alex and alex-2", [AX, AX2]), ("alex, alex-2", [AX, AX2]),
                           ("alex", [AX]), ("alex-2", [AX2])):
            self.slash("/datastash:uninstall " + text)
            self.tool("PostToolUse", "ledger_agents_list", {}, plain("web-app", "alex", "alex-2", "all,", "a b",
                                                                     ids=[OWN, AX, AX2, KEPT, X]))
            self.assertEqual(sorted(self.note()["updatedInput"]["targets"]), sorted(want), text)
            self.prompt("/datastash:uninstall")
            self.tool("PostToolUse", "ledger_agents_list", {}, plain("web-app", "alex", "alex-2", "all,",
                                                                     ids=[OWN, AX, AX2, KEPT]))
            self.prompt(text)
            self.assertEqual(sorted(self.note()["updatedInput"]["targets"]), sorted(want), text)

    def test_all_with_any_other_token_or_stray_separator_is_refused_whole(self):
        """1.0.11 confirm review HIGH (PM ruling): typed "all" anywhere must be the whole input. "all and", "and all",
        "all,", ", all" and "all" with any other word refuse the command; nothing falls back to this session's agent."""
        bad = ("all and", "and all", "all,", ", all", "all all", "all this", "this and all", "all alpha", "alpha all",
               "all, and", "and, all", "ALL AND", "And All", ",all", "all ,", "all and alpha")
        for lst in (LIST, self.OLD_LIST):
            for text in bad:
                self.assertIsNone(picks_of(text, lst, OWN), (text, len(lst["agents"])))
                for slash in (False, True):
                    if slash:
                        self.slash("/datastash:uninstall " + text)
                        shown = self.tool("PostToolUse", "ledger_agents_list", {}, lst)["systemMessage"]
                    else:
                        self.prompt("/datastash:uninstall")
                        self.tool("PostToolUse", "ledger_agents_list", {}, lst)
                        v, _ = self.prompt(text)
                        self.assertEqual(v["decision"], "block", text)
                        shown = v["reason"]
                    self.assertIn("Nothing was removed.", shown, (text, slash))
                    self.assertNotIn("remove all", shown, (text, slash))  # no confirm step is opened
                    self.assertEqual(self.note()["permissionDecision"], "deny", (text, slash))
                    self.prompt("remove all 3")  # no step is open, so this confirms nothing
                    self.assertEqual(self.note()["permissionDecision"], "deny", (text, slash))
        self.assertTrue(os.path.exists(ds.key_path("web-app")))
        for text in ("all", " ALL ", "all\t", "\u00a0all"):  # "all" alone (whitespace only) still asks to confirm
            self.assertEqual(picks_of(text, LIST, OWN), "confirm", repr(text))
        self.assertEqual(picks_of("this", LIST, OWN), [OWN])  # only "this" typed alone picks this session's agent
        self.assertIsNone(picks_of("this", LIST, None))
        self.assertEqual(picks_of("", LIST, OWN), "none")  # empty input picks nothing (no default target)

    def test_low1_punctuation_beside_a_word_reads_as_the_word(self):
        for name, word in (("all!", "all"), ("all;", "all"), ("(all)", "all"), ("al'l", "all"), ("a,ll", "all"),
                           ("this?", "this"), ("t,his", "this"), ("(and)", "and")):
            self.assertIn(word, ds._old_words(name), name)
        lst = plain("web-app", "x", "this?", ids=[OWN, X, KEPT])
        self.assertIsNone(picks_of("this", lst, OWN))  # never this session's own while "this?" is listed
        self.assertEqual(picks_of("all", lst, OWN), "confirm")
        self.assertEqual(picks_of("x and web-app", lst, OWN), [X, OWN])  # LOW 3: "and" stays a separator

    def test_all_in_make_private_or_public(self):
        lst = plain("web-app", "alex", "alex-2", "all,", "\u13aall", ids=[OWN, AX, AX2, KEPT, X])
        self.assertIsNone(self.prompt("make all public")[0])  # "all" alone is never a name here: an ordinary prompt
        self.prompt("make alex and alex-2 private")
        out = self.tool("PostToolUse", "ledger_agents_list", {}, plain("web-app", "alex", "alex-2", "all,",
                                                                       ids=[OWN, AX, AX2, KEPT]))
        self.assertEqual(sorted(self.note()["updatedInput"]["targets"]), sorted([AX, AX2]))
        self.assertIsNotNone(out)
        self.prompt("make alex all, public")
        out = self.tool("PostToolUse", "ledger_agents_list", {}, lst)
        self.assertIn("Nothing was changed.", out["systemMessage"])
        self.assertNotIn("agents page", out["systemMessage"])
        self.assertEqual(self.note()["permissionDecision"], "deny")


class RealSlashShape(Base):
    """Review 1 HIGH 1: the real Claude Code 2.1.330 entry of a typed slash command passes; nothing else like it does."""

    def setUp(self):
        super().setUp()
        self.installed()

    def note_after(self, **change):
        self.slash("/datastash:uninstall alpha beta web-app", **change)
        self.tool("PostToolUse", "ledger_agents_list", {}, LIST)
        return self.tool("PreToolUse", "ledger_agents_note", {})

    def test_the_fixture_is_the_real_shape(self):
        own, meta = real_entries("datastash:uninstall")
        self.assertEqual(own["message"]["content"], "<command-message>datastash:uninstall</command-message>\n"
                         "<command-name>/datastash:uninstall</command-name>\n<command-args>alpha, beta</command-args>")
        self.assertEqual((own["origin"], own["entrypoint"], own["isSidechain"], "promptSource" in own, own.get("isMeta")),
                         ({"kind": "human"}, "cli", False, False, None))
        self.assertTrue(meta["isMeta"])  # the expanded command text: never the typed entry
        bare, _ = real_entries("datastash:install")
        self.assertNotIn("<command-args>", bare["message"]["content"])  # no args: no args tag at all
        self.assertTrue(ds._command_origin(own) and ds._command_origin(bare))
        self.assertTrue(ds.CMD_TAG_RE.fullmatch(own["message"]["content"]) and ds.CMD_TAG_RE.fullmatch(bare["message"]["content"]))

    def test_the_real_entry_passes_and_the_list_then_typed_names_works_after_a_bare_command(self):
        got = self.note_after()["hookSpecificOutput"]
        self.assertEqual(got["updatedInput"]["targets"], sorted([OWN, ALPHA, BETA]))
        self.slash("/datastash:uninstall")
        self.tool("PostToolUse", "ledger_agents_list", {}, LIST)
        self.prompt("alpha")
        self.assertEqual(self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["updatedInput"]["targets"], [ALPHA])

    def test_anything_else_is_refused(self):
        for change in ({"entrypoint": "sdk-cli"}, {"entrypoint": None}, {"promptSource": "sdk"},
                       {"promptSource": "typed"}, {"isMeta": True}, {"isSidechain": True}, {"isSidechain": None},
                       {"origin": {"kind": "task-notification"}}, {"origin": None}, {"turnOrigin": "system"}):
            out = self.note_after(**change)
            self.assertEqual(out["hookSpecificOutput"].get("permissionDecision"), "deny", change)
        self.assertTrue(os.path.exists(ds.key_path("web-app")))

    def chains(self, chain, app="STAND_IN_APP_CHAIN"):
        """XLI-420: the hooks see this process chain (test/fixtures/desktop-process-chains.json) and desktop app part."""
        with open(os.path.join(FIXTURES, "desktop-process-chains.json")) as f:
            fx = json.load(f)
        saved = (ds.process_chain, ds.DESKTOP_APP_CHAIN)
        self.addCleanup(lambda: (setattr(ds, "process_chain", saved[0]), setattr(ds, "DESKTOP_APP_CHAIN", saved[1])))
        ds.process_chain = lambda: fx[chain] if isinstance(chain, str) else chain
        ds.DESKTOP_APP_CHAIN = tuple(tuple(x) for x in fx[app]) if app else ()

    def desktop_entry(self):
        with open(os.path.join(FIXTURES, "desktop-code-tab-2.1.332.jsonl")) as f:
            own = [json.loads(x) for x in f if x.strip()][0]
        self.assertEqual((own["origin"], own["entrypoint"], own["turnOrigin"], "promptSource" in own, own["isSidechain"]),
                         ({"kind": "human"}, "claude-desktop", "human", False, False))
        return {k: own[k] for k in ("entrypoint", "turnOrigin", "version", "userType")}

    def test_the_desktop_code_tab_entry_passes(self):
        """XLI-420: the real entry the Claude desktop app's Code tab writes for a typed slash command (Claude Code 2.1.332
        as the desktop host) passes when the hook's process chain is the desktop app's (stand-in until the capture)."""
        keep = self.desktop_entry()
        self.chains("desktop_stand_in")
        got = self.note_after(**keep)["hookSpecificOutput"]
        self.assertEqual(got["updatedInput"]["targets"], sorted([OWN, ALPHA, BETA]))
        self.assertFalse(ds._command_origin(dict(keep, origin={"kind": "human"}, isSidechain=False)))  # entry alone: no

    def test_review_1_headless_and_nested_slash_commands_are_refused(self):
        """Review 1 HIGH: the same slash-command entry from a headless or nested run (an agent's shell), a detached run,
        a terminal chain, an unreadable chain, or the shipped empty desktop part, is refused."""
        keep = self.desktop_entry()
        for chain, app in (("headless_review_1", "STAND_IN_APP_CHAIN"), ("nested_no_print", "STAND_IN_APP_CHAIN"),
                           ("detached", "STAND_IN_APP_CHAIN"), ("terminal", "STAND_IN_APP_CHAIN"),
                           (None, "STAND_IN_APP_CHAIN"), ("desktop_stand_in", None)):
            self.chains(chain, app)
            out = self.note_after(**keep)
            self.assertEqual(out["hookSpecificOutput"].get("permissionDecision"), "deny", chain)

    def test_the_terminal_entry_is_unchanged_on_any_chain(self):
        self.chains("headless_review_1")
        got = self.note_after()["hookSpecificOutput"]
        self.assertEqual(got["updatedInput"]["targets"], sorted([OWN, ALPHA, BETA]))

    def test_the_desktop_entrypoint_without_the_person_stamp_is_refused(self):
        """XLI-420: on the desktop chain, a desktop-entrypoint entry an agent or tool could make (no human origin, a side
        chain, meta, another turn origin, a promptSource) is still refused."""
        self.chains("desktop_stand_in")
        for change in ({"origin": None}, {"origin": {"kind": "task-notification"}}, {"turnOrigin": "sdk"},
                       {"promptSource": "sdk"}, {"isMeta": True}, {"isSidechain": True}):
            out = self.note_after(**dict({"entrypoint": "claude-desktop", "turnOrigin": "human"}, **change))
            self.assertEqual(out["hookSpecificOutput"].get("permissionDecision"), "deny", change)

    def test_the_older_tag_order_and_a_name_that_differs_are_refused(self):
        for content in ("<command-name>/datastash:uninstall</command-name>\n<command-message>datastash:uninstall"
                        "</command-message>\n<command-args>alpha beta web-app</command-args>",
                        "<command-message>datastash:install</command-message>\n<command-name>/datastash:uninstall"
                        "</command-name>\n<command-args>alpha beta web-app</command-args>"):
            out = self.note_after(message={"role": "user", "content": content})
            self.assertEqual(out["hookSpecificOutput"].get("permissionDecision"), "deny")


class NothingLeft(Base):
    """Review 1 MEDIUM and LOW: the uninstall turn's own hooks make no folder again; no login path; the rotated log and
    the workbook's status copy go."""

    def remove_all(self):
        self.installed()
        self.answers[ds.UNINSTALL_SELF_PATH] = (200, {"result": "removed", "run_id": RUN})
        self.slash("/datastash:uninstall")
        self.tool("PostToolUse", "ledger_agents_list", {}, dict(LIST, agents=LIST["agents"][:1]))
        self.prompt("this")
        got = self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["updatedInput"]
        out = self.tool("PostToolUse", "ledger_agents_note", got, {"run_id": RUN, "kind": "uninstall", "targets": 1,
                                                                    "removed": [], "this_agent_last": True})
        self.assertEqual(out["systemMessage"], "Done. Datastash is uninstalled from web-app. Your datastash connector in "
                         "Claude is unchanged.")
        self.assertFalse(os.path.exists(ds.CONF))

    def test_the_uninstall_turns_own_hooks_make_no_folder_again(self):
        self.remove_all()
        base = {"session_id": "s1", "cwd": self.folder, "transcript_path": self.transcript("s1")}
        self.tool("PostToolUse", "ledger_agents_note", {}, {"run_id": RUN})
        self.hook(ds.cmd_status_check, dict(base, hook_event_name="Stop"))
        self.hook(ds.cmd_agent_tool, dict(base, hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": "ls"}))
        self.hook(ds.cmd_prompt, dict(base, hook_event_name="UserPromptSubmit", prompt="thanks", prompt_id=str(uuid.uuid4())))
        ds.log("web-app", "a late line from the listener's hello thread")
        ds.log("status", "status-check: a late line")
        self.assertFalse(os.path.exists(ds.CONF))
        self.slash("/datastash:install")  # the person's own install sets it up again
        self.assertTrue(os.path.isdir(ds.CONF))

    def test_a_late_log_line_for_a_removed_agent_makes_no_file(self):
        ds.save_agent("other", str(uuid.UUID(int=5)), KEY, WB, self.folder + "-2")
        self.installed()
        ds.remove_agent_here("web-app")
        ds.log("web-app", "late")
        self.assertFalse(os.path.exists(ds.log_path("web-app")))
        self.assertTrue(os.path.isdir(ds.STATE))

    def test_the_rotated_log_and_the_workbooks_status_copy_go(self):
        other_wb = "99999999-2222-3333-4444-555555555555"
        ds.save_agent("other", str(uuid.UUID(int=5)), KEY, other_wb, self.folder + "-2")
        self.installed()
        for p in (ds.log_path("web-app") + ".1", ds.status_cache_path(WB), ds.status_cache_path(other_wb)):
            ds.write_private(p, "x")
        ds.remove_agent_here("web-app")
        self.assertFalse(os.path.exists(ds.log_path("web-app") + ".1"))
        self.assertFalse(os.path.exists(ds.status_cache_path(WB)))  # no agent here uses that workbook any more
        self.assertTrue(os.path.exists(ds.status_cache_path(other_wb)))  # the other agent's stays

    def test_a_shared_workbooks_status_copy_stays_while_an_agent_uses_it(self):
        ds.save_agent("other", str(uuid.UUID(int=5)), KEY, WB, self.folder + "-2")
        self.installed()
        ds.write_private(ds.status_cache_path(WB), "x")
        ds.remove_agent_here("web-app")
        self.assertTrue(os.path.exists(ds.status_cache_path(WB)))

    def test_no_login_path_is_left(self):
        for text in (ds.LOGIN_STEP, ds.notice_text("waiting", agent={"name": "x"})):
            self.assertNotIn("datastash login", text)
            self.assertNotIn(ds.AGENTS_URL, text)
        self.assertFalse(hasattr(ds, "LOGIN_FIRST"))
        src = open(ds.__file__).read()
        keep = src[src.index("def cmd_keep("):src.index("def ", src.index("def cmd_keep(") + 10)]
        self.assertNotIn("cmd_login(", keep)


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

    def test_each_session_s_standby_lock_goes_with_its_agent(self):
        """XLI-374: the standby lock (one per session, its name from the session id) is on the cleanup list too."""
        ds.save_agent("other", str(uuid.UUID(int=5)), KEY, WB, self.folder + "-2")
        self.installed()
        mine = [ds.standby_path("web-app", sid) for sid in ("s1", "s2")]
        theirs = ds.standby_path("other", "s1")
        stray = os.path.join(ds.STATE, "agent-web-app.standby-notours.listener.lock")
        for p in mine + [theirs, stray]:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            ds.write_private(p, "x\n")
        self.assertFalse(ds.remove_agent_here("web-app"))
        self.assertEqual([p for p in mine if os.path.exists(p)], [])
        self.assertTrue(os.path.exists(theirs))  # another agent's standby stays
        self.assertTrue(os.path.exists(stray))  # only names of the fixed standby form

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


# The owner's first real install (plugin 1.0.1, 11-23, a Mac, the home folder): the model's own note inputs, exactly.
REAL_NOTE_1 = {"kind": "install", "list_id": "7fe71587-7b74-4a3c-8c0a-750ee057dd41", "targets": [],
               "agent_id": "08514494-d4b4-4cc5-a84e-1433390bd078"}
REAL_NOTE_2 = {"kind": "install", "agent_id": "c02bb852-9f70-4d18-8149-ee8f167dff25"}


KEY_A, ANSWER_A = "ldgr_ag_" + "a" * 40, "a" * 43
KEY_B, ANSWER_B = "ldgr_ag_" + "b" * 40, "b" * 43


def two_agents_elsewhere(t):
    """alpha and beta set up on this computer, each in its own folder (key and answer key); the test's session folder
    has no agent."""
    root = os.path.dirname(t.folder)
    for name, aid, key, ans in (("alpha", ALPHA, KEY_A, ANSWER_A), ("beta", BETA, KEY_B, ANSWER_B)):
        os.makedirs(os.path.join(root, name))
        ds.save_agent(name, aid, key, WB, os.path.join(root, name))  # set up here, in its own folder
        c = ds.read_json(ds.conf_path(name), {})
        c["answer_store"] = ds.save_agent_answer(name, ans)
        ds.write_private(ds.conf_path(name), json.dumps(c))
    t.assertIsNone(ds._own({"cwd": t.folder, "session_id": "s1"}))  # this session: no agent key of its own


class UninstallNoKey(Base):
    """1.0.13 (found on the owner's computer): /datastash:uninstall typed in a session that is no agent of its own (the
    person's main or SPM session, a folder with no agent) was refused before anything was sent, with "Type
    /datastash:install again", which would make one more agent. Now the person's typed names are signed by one of their
    agents still set up on this computer (its own answer key; the proof bound to the named agent ids), and only when
    there is none is the fixed line said, which never sends the person to /datastash:install."""

    def setUp(self):
        super().setUp()
        two_agents_elsewhere(self)

    def note(self, text, source="typed"):
        self.prompt("/datastash:uninstall")
        self.tool("PostToolUse", "ledger_agents_list", {}, LIST)
        _, pid = self.prompt(text, source=source)
        return self.tool("PreToolUse", "ledger_agents_note", {"targets": [OWN], "typed_proof": {"v": 1}}), pid

    def test_named_agents_are_removed_with_the_typed_proof_of_an_agent_on_this_computer(self):
        out, pid = self.note("beta")
        got = out["hookSpecificOutput"]["updatedInput"]
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        self.assertEqual(got["targets"], [BETA])  # the person's names, never the model's targets or proof
        self.assertTrue(proof_ok(got["typed_proof"], "uninstall", [BETA], pid, "beta", own=ALPHA, answer=ANSWER_A))
        self.assertEqual(sorted(got), ["kind", "list_id", "targets", "typed_proof"])
        self.assertNotIn(KEY_A, json.dumps(out))
        self.assertNotIn(ANSWER_A, json.dumps(out))  # the key never reaches the model
        status = "/api/v1/agent/uninstall/" + RUN
        self.answers[status] = (200, {"run_id": RUN, "kind": "uninstall", "total": 1, "confirmed": [], "not_confirmed": ["beta"]})
        line = self.tool("PostToolUse", "ledger_agents_note", got, {
            "run_id": RUN, "kind": "uninstall", "targets": 1, "removed": ["beta"], "this_agent_last": False,
            "status_path": status})["systemMessage"]
        # 1.0.14: beta is set up on this computer, so it is cleaned up here at once (its dead key's one confirm)
        self.assertIn(("POST", ds.UNINSTALL_CONFIRM_PATH, KEY_B, {}), self.calls)
        self.assertFalse(any(c[:2] == ("POST", ds.UNINSTALL_SELF_PATH) for c in self.calls))  # the signer stays
        self.assertEqual(line, "Done. Datastash is uninstalled from beta. Your datastash connector in Claude is unchanged.")
        self.assertFalse(os.path.exists(ds.key_path("beta")))
        self.assertTrue(os.path.exists(ds.key_path("alpha")))

    def test_every_agent_on_this_computer_named_the_signer_goes_last_on_its_own_key(self):
        out, pid = self.note("alpha, beta")
        got = out["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(got["targets"], sorted([ALPHA, BETA]))
        self.assertTrue(proof_ok(got["typed_proof"], "uninstall", [ALPHA, BETA], pid, "alpha, beta", own=ALPHA,
                                 answer=ANSWER_A))
        status = "/api/v1/agent/uninstall/" + RUN
        self.answers[status] = (200, {"run_id": RUN, "kind": "uninstall", "total": 2, "confirmed": [], "not_confirmed": ["alpha", "beta"]})
        self.answers[ds.UNINSTALL_SELF_PATH] = (200, {"result": "removed", "run_id": RUN})
        line = self.tool("PostToolUse", "ledger_agents_note", got, {
            "run_id": RUN, "kind": "uninstall", "targets": 2, "removed": ["beta"], "this_agent_last": True,
            "status_path": status})["systemMessage"]
        self.assertIn(("POST", ds.UNINSTALL_SELF_PATH, KEY_A, {"run_id": RUN}), self.calls)  # the signer, last
        self.assertFalse(os.path.exists(ds.key_path("alpha")))  # and cleaned up here
        self.assertFalse(os.path.exists(ds.key_path("beta")))  # 1.0.14: beta, set up here too, at once
        self.assertLess(self.calls.index(("POST", ds.UNINSTALL_CONFIRM_PATH, KEY_B, {})),
                        self.calls.index(("POST", ds.UNINSTALL_SELF_PATH, KEY_A, {"run_id": RUN})))
        self.assertEqual(line, "Done. Datastash is uninstalled from alpha and beta. Your datastash connector in Claude is "
                               "unchanged.")
        self.assertNotIn("/datastash:install", line)

    def test_remove_all_from_a_session_with_no_key(self):
        self.prompt("/datastash:uninstall all")
        self.tool("PostToolUse", "ledger_agents_list", {}, LIST)
        _, pid = self.prompt("remove all 3")
        got = self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["updatedInput"]
        self.assertTrue(proof_ok(got["typed_proof"], "uninstall", [OWN, ALPHA, BETA], pid, "remove all 3", own=ALPHA,
                                 answer=ANSWER_A))

    def test_a_model_made_uninstall_is_still_refused(self):
        out, _ = self.note("beta", source="sdk")  # a headless or model-made prompt: no person origin
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(out["systemMessage"], ds.NOT_PERSON_LINE)
        self.assertNotIn("updatedInput", out["hookSpecificOutput"])
        self.assertTrue(os.path.exists(ds.key_path("alpha")) and os.path.exists(ds.key_path("beta")))
        none = self.tool("PreToolUse", "ledger_agents_note", {"kind": "uninstall", "targets": [BETA]}, sid="s2")
        self.assertEqual(none["systemMessage"], ds.NO_STEP_LINE)  # no typed command in that session: nothing to sign

    def test_an_agent_not_on_the_list_never_signs(self):
        lst = dict(LIST, agents=[a for a in LIST["agents"] if a["agent_id"] != ALPHA])
        self.prompt("/datastash:uninstall")
        self.tool("PostToolUse", "ledger_agents_list", {}, lst)
        _, pid = self.prompt("web-app")
        got = self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["updatedInput"]
        self.assertTrue(proof_ok(got["typed_proof"], "uninstall", [OWN], pid, "web-app", own=BETA, answer=ANSWER_B))

    def test_no_agent_on_this_computer_says_what_to_do_and_never_install(self):
        for s in ("alpha", "beta"):
            ds._drop(ds.key_path(s))  # a record left here with no key: nothing on this computer can sign
        v, _ = self.prompt("/datastash:uninstall")  # 1.0.16: said at once, plainly, before any step
        self.assertEqual((v["decision"], v["reason"]), ("block", "Datastash isn't installed on this agent. Nothing was "
                                                                  "removed."))
        self.assertEqual(ds.NO_SIGNER_LINE, ds.NOT_INSTALLED_LINE)
        self.assertNotIn("/datastash:install", ds.NO_SIGNER_LINE)
        self.assertFalse(any(c[0] == "POST" for c in self.calls))


class UninstallNoKeyRealHooks(Base):
    """The same key-less uninstall, end to end through the plugin's real hook commands as Claude Code runs them (a
    separate python3 process per hook, the hook input on stdin, the real slash-command transcript entries): the prompt
    hook opens the step, the list's PostToolUse shows it, the typed pick is taken, and the note's PreToolUse comes back
    signed by an agent on this computer. No network: it stops before the note goes out."""

    def run_hook(self, args, inp):
        env = dict(os.environ, DATASTASH_CONFIG_DIR=ds.CONF, DATASTASH_NO_NETWORK="1")
        env.pop("CLAUDE_PROJECT_DIR", None)
        r = subprocess.run([sys.executable, "-I", BIN, *args], input=json.dumps(inp), capture_output=True, text=True,
                           env=env, timeout=30, cwd=self.folder)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def test_a_keyless_session_uninstall_through_the_real_hooks(self):
        two_agents_elsewhere(self)
        with open(os.path.join(FIXTURES, "slash-command-2.1.330-hooks.json")) as f:
            shape = dict([h for h in json.load(f) if h["prompt"].startswith("/datastash:uninstall")][0], prompt="/datastash:uninstall")
        sid, tp = "s1", self.transcript("s1")
        pid = str(uuid.uuid4())
        v = self.run_hook(["prompt"], dict(shape, session_id=sid, cwd=self.folder, prompt_id=pid, transcript_path=tp))
        self.assertIn("ledger_agents_list", v["hookSpecificOutput"]["additionalContext"])
        own, meta = real_entries("datastash:uninstall")
        with open(tp, "a") as f:
            c = "<command-message>datastash:uninstall</command-message>\n<command-name>/datastash:uninstall</command-name>"
            f.write(json.dumps(dict(own, promptId=pid, sessionId=sid, message={"role": "user", "content": c}, timestamp=time.strftime(
                "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()))) + "\n" + json.dumps(dict(meta, promptId=pid, sessionId=sid)) + "\n")
        tool = {"session_id": sid, "cwd": self.folder, "transcript_path": tp}
        shown = self.run_hook(["agent-tool"], dict(tool, hook_event_name="PostToolUse", tool_name=TOOL + "ledger_agents_list",
                                                   tool_input={}, tool_response=[{"type": "text", "text": json.dumps(LIST)}]))
        self.assertTrue(shown["systemMessage"].startswith("Your datastash agents:"), shown)
        pid = str(uuid.uuid4())
        v = self.run_hook(["prompt"], {"session_id": sid, "prompt": "beta", "hook_event_name": "UserPromptSubmit",
                                       "cwd": self.folder, "prompt_id": pid, "transcript_path": tp})
        self.assertIn('kind "uninstall"', v["hookSpecificOutput"]["additionalContext"])
        self.typed(sid, "beta", pid)
        out = self.run_hook(["agent-tool"], dict(tool, hook_event_name="PreToolUse", tool_name=TOOL + "ledger_agents_note",
                                                 tool_input={"kind": "uninstall", "targets": [OWN]}))
        got = out["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(got["targets"], [BETA])
        self.assertTrue(proof_ok(got["typed_proof"], "uninstall", [BETA], pid, "beta", own=ALPHA, answer=ANSWER_A))
        self.assertNotIn("/datastash:install", json.dumps(out))
        for secret in (KEY_A, ANSWER_A, KEY_B, ANSWER_B):
            self.assertNotIn(secret, json.dumps(out) + json.dumps(shown) + json.dumps(v))


class UninstallNoBox(Base):
    def test_a_computer_with_no_agent_at_all_says_what_to_do(self):
        v, _ = self.prompt("/datastash:uninstall")
        self.assertEqual(v["reason"], ds.NOT_INSTALLED_LINE)
        self.assertNotIn("did not answer", v["reason"])


class HomeFolderInstall(Base):
    """XLI-390 install fix. 1.0.1 run in the home folder (the folder an agent can't be linked to) named the agent after
    the computer's user, could not find it again (so ledger_agents_note was denied with "datastash refused it" before
    anything was sent) and made a new "-2" agent on every retry. Now: a sensible name, the session's own agent found
    again (note sent with the plugin's own list id, targets and proof), one line with the reason on any refusal."""

    def setUp(self):
        super().setUp()
        self.home_saved = ds.HOME
        self.folder = os.path.join(os.path.dirname(self.folder), "home-user")  # the person's home folder (its name is the computer's user)
        os.makedirs(self.folder)
        ds.HOME = self.folder
        self.user = mock.patch("getpass.getuser", return_value="home-user")  # 1.0.16: the home agent is the user's name
        self.user.start()
        self.addCleanup(self.user.stop)

    def tearDown(self):
        ds.HOME = self.home_saved
        super().tearDown()

    def mint(self, name, agent_id=OWN, sid="s1"):
        self.tool("PostToolUse", "ledger_agent_register", {"name": name}, {"agent_id": agent_id, "notes_tabs": [{"workbook_id": WB}]}, sid=sid)
        self.tool("PreToolUse", "ledger_agent_key", {"name": name, "agent_id": agent_id, "only_if_no_key": True}, sid=sid)
        self.answers[ds.COLLECT_PATH] = (200, {"agent_id": agent_id, "name": name, "workbook_id": WB, "key": KEY,
                                               "answer_key": ANSWER})
        out = self.tool("PostToolUse", "ledger_agent_key", {"name": name}, {"collect": "ready", "mint_id": MINT, "name": name}, sid=sid)
        self.assertEqual(out["systemMessage"], ds.MINT_LINE.format(name=name))

    def list_and_note(self, real_input, pid, sid="s1", own=OWN):
        self.assertIsNone(self.tool("PostToolUse", "ledger_agents_list", {}, LIST, sid=sid))
        note = self.tool("PreToolUse", "ledger_agents_note", real_input, sid=sid)["hookSpecificOutput"]
        self.assertNotEqual(note.get("permissionDecision"), "deny", note)
        got = note["updatedInput"]
        others = sorted({OWN, ALPHA, BETA} - {own})
        self.assertEqual(sorted(got), ["kind", "list_id", "targets", "typed_proof"])  # the model's agent_id is gone
        self.assertEqual((got["kind"], got["list_id"], got["targets"]), ("install", LIST_ID, others))
        self.assertTrue(proof_ok(got["typed_proof"], "install", others, pid, "/datastash:install", own=own))
        return got

    def go(self, name):
        """The person's next session runs in this folder ("home" is the home folder)."""
        home = self.folder if os.path.realpath(self.folder) == os.path.realpath(ds.HOME) else ds.HOME
        self.folder = home if name == "home" else os.path.join(os.path.dirname(home), name)
        os.makedirs(self.folder, exist_ok=True)

    def install(self, sid, register=None, own=OWN, real=REAL_NOTE_1):
        """/datastash:install in this session: `register` is the new agent's name the plugin asks for (None: it reuses
        this session's agent and registers nothing); then the install note goes out signed by `own`."""
        v, pid = self.slash("/datastash:install", sid=sid)
        ai = v["hookSpecificOutput"]["additionalContext"]
        if register is None:
            self.assertNotIn("ledger_agent_register", ai)
        else:
            self.assertIn(f'ledger_agent_register with name "{register}"', ai)
            self.mint(register, agent_id=own, sid=sid)
        self.list_and_note(real, pid, sid=sid, own=own)

    def agents(self):
        return {a["name"]: [os.path.basename(p) for p in a.get("projects") or []] for a in ds.all_agents()}

    def test_home_install_then_a_new_session_in_web_app_makes_a_web_app_agent(self):
        self.install("s1", register="home-user")
        self.go("web-app")
        self.install("s2", register="web-app", own=ALPHA)  # not the home agent: this folder gets its own
        self.assertEqual(self.agents(), {"home-user": [], "web-app": ["web-app"]})
        self.install("s2", own=ALPHA, real=REAL_NOTE_2)  # a retry in the same session: the same agent, no -2
        self.go("web-app")
        self.install("s3", own=ALPHA)  # a later session in the folder: its linked agent
        self.go("home")
        self.install("s4")  # a later session in the home folder: the home agent (the one with no folder)
        self.assertEqual(self.agents(), {"home-user": [], "web-app": ["web-app"]})

    def cli(self, fn, args, stdin=None):
        """A datastash command run in self.folder (CLAUDE_PROJECT_DIR): (exit code, stdout)."""
        orig, env = sys.stdin, os.environ.get("CLAUDE_PROJECT_DIR")
        sys.stdin = io.StringIO(stdin or "")
        os.environ["CLAUDE_PROJECT_DIR"] = self.folder
        try:
            with redirect_stdout(io.StringIO()) as out:
                try:
                    rc = fn(args)
                except SystemExit as e:
                    rc = e.code
        finally:
            sys.stdin = orig
            if env is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = env
        return rc, out.getvalue()

    def test_a_repo_under_home_never_takes_the_home_agent(self):
        """1.0.18 real e2e step 4: in the home folder a status write found no linked agent, so the AI ran `datastash link`
        there; every repo under the home folder (~/code/web-app) then started as the home agent and its install
        registered nothing. Now the home link is refused, an old one never covers a subfolder, and the status write
        finds the home folder's own agent without a link."""
        self.install("s1", register="home-user")
        home = self.folder
        rc, _ = self.cli(ds.cmd_link, ["home-user", "--workbook", WB])
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.agents(), {"home-user": []})  # nothing was linked
        rc, out = self.cli(ds.cmd_status_set, ["-"], stdin=json.dumps({"doing": "Installed datastash."}))
        self.assertEqual(rc, 0, out)
        self.assertIn('status saved for "home-user"', out)
        c = ds.read_json(ds.conf_path("home-user"), {})  # a home link already on this computer (an earlier build)
        c["projects"] = [home]
        ds.write_private(ds.conf_path("home-user"), json.dumps(c))
        self.folder = os.path.join(home, "code", "web-app")
        os.makedirs(self.folder)
        self.assertIsNone(ds.session_for(self.folder))  # never the home agent
        self.install("s2", register="web-app", own=ALPHA)  # its own agent, as a repo beside home would get
        self.assertEqual(self.agents(), {"home-user": ["home-user"], "web-app": ["web-app"]})
        self.assertEqual(ds.session_for(home)["name"], "home-user")  # the home folder itself keeps its agent

    def test_folder_install_then_home_makes_a_home_agent(self):
        self.go("web-app")
        self.install("s1", register="web-app")
        self.go("home")
        self.install("s2", register="home-user", own=ALPHA)  # the folder's agent is never the home folder's
        self.install("s2", own=ALPHA, real=REAL_NOTE_2)  # retry, same session
        self.install("s3", own=ALPHA)  # a new session in home: the one agent with no folder
        self.assertEqual(self.agents(), {"home-user": [], "web-app": ["web-app"]})

    def test_two_folders_each_make_their_own_agent(self):
        self.go("web-app")
        self.install("s1", register="web-app")
        self.go("api")
        self.install("s2", register="api", own=ALPHA)
        self.install("s2", own=ALPHA, real=REAL_NOTE_2)
        self.assertEqual(self.agents(), {"web-app": ["web-app"], "api": ["api"]})
        self.go("web-app")
        self.install("s3")

    def test_two_folders_after_a_home_install(self):
        self.install("s1", register="home-user")
        self.go("web-app")
        self.install("s2", register="web-app", own=ALPHA)
        self.go("api")
        self.install("s3", register="api", own=BETA)
        self.assertEqual(self.agents(), {"home-user": [], "web-app": ["web-app"], "api": ["api"]})

    def test_no_folder_fallback_in_a_folder_it_can_link(self):
        self.install("s1", register="home-user")
        self.go("web-app")
        inp = {"session_id": "s2", "cwd": self.folder, "hook_event_name": "PreToolUse"}
        self.assertIsNone(ds.session_agent(inp))
        self.assertIsNone(ds._own(inp))
        self.go("home")
        self.assertEqual(ds.session_agent(dict(inp, cwd=self.folder))["name"], "home-user")  # home: the fallback holds

    def test_uninstall_drops_the_session_record(self):
        self.install("s1", register="home-user")
        self.go("web-app")
        self.install("s2", register="web-app", own=ALPHA)
        rec = lambda sid: os.path.exists(os.path.join(ds.STATE, f"session-agent-{sid}.json"))
        self.assertTrue(rec("s1") and rec("s2"))
        self.assertFalse(ds.remove_agent_here("home-user"))  # web-app is left
        self.assertFalse(rec("s1"))
        self.assertTrue(rec("s2"))
        self.assertTrue(ds.remove_agent_here("web-app"))
        self.assertFalse(rec("s2"))

    def test_fresh_home_folder_install_sends_the_note_and_a_retry_reuses_the_agent(self):
        v, pid = self.slash("/datastash:install")
        ai = v["hookSpecificOutput"]["additionalContext"]
        self.assertIn('ledger_agent_register with name "home-user"', ai)  # 1.0.16: the computer's user, never "agent"
        self.mint("home-user")
        self.list_and_note(REAL_NOTE_1, pid)
        # The retry, in the same session: no new agent, and the note goes again (the model's call 2 shape: no list id).
        v2, pid2 = self.slash("/datastash:install")
        ai2 = v2["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("ledger_agent_register", ai2)
        self.assertNotIn("home-user-2", ai2)
        self.list_and_note(REAL_NOTE_2, pid2)
        self.assertEqual([a["name"] for a in ds.all_agents()], ["home-user"])

    def test_the_session_agent_name_wins_over_the_folder(self):
        with open(os.path.join(FIXTURES, "slash-command-2.1.330-hooks.json")) as f:
            shape = [h for h in json.load(f) if h["prompt"] == "/datastash:install"][0]
        v = self.hook(ds.cmd_prompt, dict(shape, session_id="s1", cwd=self.folder, prompt_id=str(uuid.uuid4()),
                                          transcript_path=self.transcript("s1"), agent_type="SPM"))
        self.assertIn('ledger_agent_register with name "spm"', v["hookSpecificOutput"]["additionalContext"])  # lowercase

    def test_the_agent_this_session_listens_for_is_reused(self):
        ds.save_agent("SPM", OWN, KEY, WB, self.folder)  # the computer's one agent with no folder (home: never linked)
        c = ds.read_json(ds.conf_path("spm"), {})
        c["answer_store"] = ds.save_agent_answer("spm", ANSWER)
        ds.write_private(ds.conf_path("spm"), json.dumps(c))
        self.assertEqual(ds.read_json(ds.conf_path("spm"), {}).get("projects"), None)
        v, pid = self.slash("/datastash:install")
        self.assertNotIn("ledger_agent_register", v["hookSpecificOutput"]["additionalContext"])
        self.list_and_note(REAL_NOTE_1, pid)

    def test_a_linkable_folder_still_takes_its_own_name(self):
        here = os.path.join(os.path.dirname(self.folder), "web-app")
        self.folder = here
        v, _ = self.slash("/datastash:install")
        self.assertIn('ledger_agent_register with name "web-app"', v["hookSpecificOutput"]["additionalContext"])

    def test_every_refusal_is_one_line_with_its_reason(self):
        out = self.tool("PreToolUse", "ledger_agents_note", REAL_NOTE_2)  # no command open in this session
        self.assertEqual(out["systemMessage"], ds.NO_STEP_LINE)
        self.slash("/datastash:install")
        self.tool("PostToolUse", "ledger_agents_list", {}, LIST)
        out = self.tool("PreToolUse", "ledger_agents_note", REAL_NOTE_1)  # no key collected on this computer
        self.assertEqual(out["systemMessage"], ds._plain_typed(ds.NO_OWN_AGENT_LINE))
        _, pid = self.slash("/datastash:install")
        self.mint("home-user")
        got = self.list_and_note(REAL_NOTE_1, pid)
        out = self.tool("PostToolUse", "ledger_agents_note", got, {"code": "not_typed", "message": "Only the person's own "
                        "typed answer, through the datastash plugin, sends agent notes. Nothing changed."})
        self.assertEqual(out["systemMessage"], "Not done: datastash refused it (not_typed: Only the person's own typed "
                         "answer, through the datastash plugin, sends agent notes). Nothing changed.")
        self.assertEqual(out["systemMessage"].count("\n"), 0)

    def test_the_connector_line_is_the_typed_sign_in(self):
        self.assertEqual(ds.NO_CONNECTOR_LINE, "Type **datastash sync you@email.com**, then **/datastash:install** again.")
        v, _ = self.slash("/datastash:install")
        self.assertIn(ds.NO_CONNECTOR_LINE, v["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("Settings, Connectors", v["hookSpecificOutput"]["additionalContext"])


class FreshHome(Base):
    """Release 1.0.2 review HIGH: on a fresh macOS/Linux computer (empty HOME, no datastash login) the connector's
    helper sent "Bearer signed-out", which hid Claude Code's /mcp Authenticate, and no hook starts a sign-in, so a new
    person could never sign in. Now (XLI-390: identity rides on the connector's own OAuth sign-in) the helper sends no
    header there; an earlier 0.9.x datastash login still gets its token (and the placeholder only while it fails)."""

    def helper(self, home, entry="cli"):
        """`token --header` exactly as Claude Code runs the connector's headersHelper, in a clean environment."""
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "CLAUDE_CODE_ENTRYPOINT": entry,
               "CLAUDE_CODE_MCP_SERVER_URL": ds.CONNECTOR_URL, "CLAUDE_CONFIG_DIR": os.path.join(home, ".claude"),
               "DATASTASH_CONFIG_DIR": os.path.join(home, ".config", "datastash"),  # the default under HOME, named for the harness
               "DATASTASH_NO_NETWORK": "1"}
        r = subprocess.run([sys.executable, "-I", BIN, "token", "--header"], capture_output=True, text=True, env=env,
                           timeout=20, cwd=home)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return json.loads(r.stdout)

    def test_an_empty_home_sends_no_placeholder_so_mcp_authenticate_shows(self):
        home = harness.tmpdir(self, prefix="empty-home-", dir=harness.ROOT)
        self.assertEqual(os.listdir(home), [])
        self.assertEqual(self.helper(home), {})  # no Authorization: Claude Code offers its own OAuth sign-in
        self.assertEqual(self.helper(home), {})  # and on every later run (the device mode is recorded by then)
        with open(os.path.join(home, ".config", "datastash", "signin-mode.json")) as f:
            self.assertEqual(json.load(f)["mode"], ds.SIGNIN_DEVICE)

    def test_an_old_0_9_login_still_gets_its_token_and_the_placeholder_while_it_fails(self):
        home = harness.tmpdir(self, prefix="old-login-home-", dir=harness.ROOT)
        conf = os.path.join(home, ".config", "datastash")
        os.makedirs(conf, mode=0o700)
        ds.write_private(os.path.join(conf, "login.json"), json.dumps({"store": "file", "at": 1}))
        ds.write_private(os.path.join(conf, "login.refresh"), "dsrt_" + "r" * 43 + "\n")
        tok = "dsat_" + "a" * 43
        ds.write_private(os.path.join(conf, "login.access.json"), json.dumps({"token": tok, "exp": int(time.time()) + 900}))
        self.assertEqual(self.helper(home), {"Authorization": "Bearer " + tok})  # as before
        ds.write_private(os.path.join(conf, "login.access.json"), json.dumps({"token": tok, "exp": 0}))
        os.replace(os.path.join(conf, "login.refresh"), os.path.join(home, "moved-refresh"))  # refresh can't run here
        self.assertEqual(self.helper(home), {"Authorization": "Bearer signed-out"})  # as before: a login on record

    def test_after_authenticate_install_makes_the_key_note_and_list(self):
        """The fresh-HOME path end to end: no hook sign-in at session start, the helper's {} (so /mcp Authenticate
        signs the plugin's own connector in), then /datastash:install through that connector: key, note, list."""
        self.assertFalse(ds.hooks_offer_sign_in())
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        os.environ["CLAUDE_CODE_MCP_SERVER_URL"] = ds.CONNECTOR_URL
        self.addCleanup(os.environ.pop, "CLAUDE_CODE_ENTRYPOINT", None)
        self.addCleanup(os.environ.pop, "CLAUDE_CODE_MCP_SERVER_URL", None)
        self.assertEqual(os.listdir(ds.CONF) if os.path.isdir(ds.CONF) else [], [])  # nothing datastash on this computer
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(ds.cmd_token(["--header"]), 0)
        self.assertEqual(json.loads(buf.getvalue()), {})
        self.assertEqual(ds.signin_state()["mode"], ds.SIGNIN_DEVICE)
        start = self.hook(ds.cmd_check, {"session_id": "s1", "hook_event_name": "SessionStart", "cwd": self.folder}) or {}
        self.assertNotIn("getdatastash.com/agents", json.dumps(start))  # no device code: the /mcp door is the one way in
        self.assertFalse([c for c in self.calls if c[1] == ds.DEVICE_START_PATH])
        v, pid = self.slash("/datastash:install")
        ai = v["hookSpecificOutput"]["additionalContext"]
        self.assertIn('ledger_agent_register with name "web-app"', ai)
        self.assertIn(ds.NO_CONNECTOR_LINE, ai)  # the one fallback line, if the connector is still not there
        self.assertNotIn("datastash login", ai)
        for k in ("systemMessage",):
            self.assertNotIn(k, v or {})
        self.tool("PostToolUse", "ledger_agent_register", {"name": "web-app"},
                  {"agent_id": OWN, "notes_tabs": [{"workbook_id": WB}]})
        bound = self.tool("PreToolUse", "ledger_agent_key", {"name": "web-app", "agent_id": OWN, "only_if_no_key": True})
        challenge = bound["hookSpecificOutput"]["updatedInput"]["code_challenge"]
        self.answers[ds.COLLECT_PATH] = (200, {"agent_id": OWN, "name": "web-app", "workbook_id": WB, "key": KEY,
                                               "answer_key": ANSWER})
        out = self.tool("PostToolUse", "ledger_agent_key", {"name": "web-app"},
                        {"collect": "ready", "mint_id": MINT, "name": "web-app"})
        self.assertEqual(out["systemMessage"], ds.MINT_LINE.format(name="web-app"))
        sent = [c for c in self.calls if c[1] == ds.COLLECT_PATH][0][3]
        self.assertEqual(ds.challenge_of(sent["verifier"]), challenge)
        self.assertEqual(stat.S_IMODE(os.stat(ds.key_path("web-app")).st_mode), 0o600)  # the key, mode 600
        self.assertNotIn(KEY, json.dumps(out) + json.dumps(bound))
        self.tool("PostToolUse", "ledger_agents_list", {}, LIST)
        note = self.tool("PreToolUse", "ledger_agents_note", {"kind": "install"})["hookSpecificOutput"]
        self.assertNotEqual(note.get("permissionDecision"), "deny", note)
        got = note["updatedInput"]
        self.assertEqual((got["kind"], got["list_id"], got["targets"]), ("install", LIST_ID, sorted([ALPHA, BETA])))
        self.assertTrue(proof_ok(got["typed_proof"], "install", [ALPHA, BETA], pid, "/datastash:install"))
        done = self.tool("PostToolUse", "ledger_agents_note", got, {"run_id": RUN, "kind": "install", "targets": 2,
                                                                     "status_path": "/api/v1/agent/uninstall/" + RUN})
        self.assertEqual(done["systemMessage"].split("\n")[1:5], [ds.ON_HEAD, "- web-app (public)", "- alpha (private)",
                                                                  "- beta (public)"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            ds.cmd_token(["--header"])
        self.assertEqual(json.loads(buf.getvalue()), {})  # still no placeholder: the OAuth sign-in keeps working

    def tool(self, event, name, tool_input=None, answer=None, sid="s1"):
        """The plugin's own connector (signed in by /mcp Authenticate), not a claude.ai one."""
        inp = {"session_id": sid, "hook_event_name": event, "tool_name": OWN_TOOL + name, "tool_input": tool_input or {},
               "cwd": self.folder}
        if answer is not None:
            inp["tool_response"] = [{"type": "text", "text": json.dumps(answer)}]
        return self.hook(ds.cmd_agent_tool, inp)


class FirstSessionInNewFolder(Base):
    """1.0.14 (real e2e, 11-25, bug A): in a folder Claude Code never ran in, the first session's
    ~/.claude/projects/<folder> does not exist yet when the prompt hooks run (Claude Code makes it with the transcript,
    after them), so /datastash:install and :uninstall got no note ("datastash did not answer"). That missing folder is
    accepted only when it is the one Claude Code names for the session's own cwd; every later read checks the real file
    again, so a typed check still needs the person's own typed entry in that file."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(ds.CLAUDE_DIR, "projects"), exist_ok=True)

    def new_folder_transcript(self, sid, cwd=None):
        name = re.sub(r"[^A-Za-z0-9]", "-", cwd or self.folder)
        d = os.path.join(ds.CLAUDE_DIR, "projects", name)
        self.assertFalse(os.path.lexists(d))
        return d, os.path.join(d, f"{sid}.jsonl")

    def hook_input(self, text, sid, tp, pid, cwd=None):
        return {"session_id": sid, "prompt": text, "hook_event_name": "UserPromptSubmit", "cwd": cwd or self.folder,
                "prompt_id": pid, "transcript_path": tp}

    def test_the_first_install_in_a_new_folder_gets_its_note_and_still_needs_the_typed_entry(self):
        sid, pid = "s-new-1", str(uuid.uuid4())
        d, tp = self.new_folder_transcript(sid)
        v = self.hook(ds.cmd_prompt, self.hook_input("/datastash:install", sid, tp, pid))
        self.assertIn('ledger_agent_register with name "web-app"', v["hookSpecificOutput"]["additionalContext"])
        self.assertFalse(os.path.lexists(d))  # the hook makes nothing in Claude Code's folder
        self.assertIsNone(ds._typed_entry(dict(ds.read_json(ds._cmd_path(sid), {}), sid=sid)))  # no file yet: no proof
        os.makedirs(d)
        v2 = {"type": "user", "promptId": pid, "message": {"role": "user", "content": "/datastash:install"},
              "promptSource": "typed", "origin": {"kind": "human"}, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
        with open(tp, "a") as f:
            f.write(json.dumps(v2) + "\n")
        self.assertEqual(ds._typed_entry(dict(ds.read_json(ds._cmd_path(sid), {}), sid=sid)), v2)

    def test_the_first_uninstall_in_a_new_folder_gets_its_note(self):
        sid, pid = "s-new-2", str(uuid.uuid4())
        _, tp = self.new_folder_transcript(sid)
        self.installed()  # 1.0.16: with nothing installed here the plugin says so instead (UninstallResultLines)
        v = self.hook(ds.cmd_prompt, self.hook_input("/datastash:uninstall web-app", sid, tp, pid))
        self.assertEqual(v["hookSpecificOutput"]["additionalContext"], ds.UNINSTALL_LIST_AI)

    def test_a_missing_folder_that_is_not_this_sessions_own_is_still_refused(self):
        sid = "s-new-3"
        other = os.path.join(os.path.dirname(self.folder), "elsewhere")
        _, tp = self.new_folder_transcript(sid, cwd=other)  # the folder name of another cwd
        self.assertIsNone(self.hook(ds.cmd_prompt, self.hook_input("/datastash:install", sid, tp, str(uuid.uuid4()))))
        self.assertIsNone(ds.hook_shape(self.hook_input("/datastash:install", sid, tp, str(uuid.uuid4()))))
        for bad in ("..", ".", "a/b"):  # never a path out of projects/, whatever cwd says
            self.assertIsNone(ds.hook_shape(self.hook_input("/datastash:install", sid, os.path.join(
                ds.CLAUDE_DIR, "projects", bad, f"{sid}.jsonl"), str(uuid.uuid4()))))
        no_cwd = self.hook_input("/datastash:install", sid, self.new_folder_transcript(sid)[1], str(uuid.uuid4()))
        no_cwd.pop("cwd")
        self.assertIsNone(ds.hook_shape(no_cwd))

    def test_a_new_claude_code_with_no_projects_folder_yet(self):
        root = os.path.join(ds.CLAUDE_DIR, "projects")
        aside = root + ".aside-" + uuid.uuid4().hex[:8]
        os.rename(root, aside)
        try:
            sid, pid = "s-new-6", str(uuid.uuid4())
            _, tp = self.new_folder_transcript(sid)
            self.assertIsNotNone(ds.hook_shape(self.hook_input("/datastash:install", sid, tp, pid)))
        finally:
            os.rename(aside, root)

    def test_a_link_in_place_of_the_folder_is_refused(self):
        sid = "s-new-4"
        d, tp = self.new_folder_transcript(sid)
        os.makedirs(os.path.join(os.path.dirname(self.folder), "target"))
        os.symlink(os.path.join(os.path.dirname(self.folder), "target"), d)
        self.assertIsNone(ds.hook_shape(self.hook_input("/datastash:install", sid, tp, str(uuid.uuid4()))))
        dangling = os.path.join(ds.CLAUDE_DIR, "projects", re.sub(r"[^A-Za-z0-9]", "-", self.folder) + "-x")
        os.symlink(os.path.join(os.path.dirname(self.folder), "nowhere"), dangling)  # a dangling link is no new folder
        self.assertFalse(ds._transcript_ok(os.path.join(dangling, f"{sid}.jsonl"), sid, must_exist=False,
                                           cwd=self.folder + "-x"))

    def test_the_checker_waits_for_the_folder_and_then_reads_the_real_file(self):
        sid, pid = "s-new-5", str(uuid.uuid4())
        d, tp = self.new_folder_transcript(sid)
        shape = ds.hook_shape(self.hook_input("keep this", sid, tp, pid))
        self.assertEqual(shape.cwd, self.folder)
        self.assertTrue(ds.start_check(shape, "keep", "keep this", {}))
        rec = ds.read_json(ds._pending_path(pid), {})
        self.assertEqual(rec["cwd"], self.folder)
        self.assertTrue(ds._transcript_ok(rec["tp"], sid, must_exist=False, cwd=rec["cwd"]))
        self.assertFalse(ds._transcript_ok(rec["tp"], sid))  # the real read needs the real file


class UninstallCleansThisComputer(Base):
    """1.0.14 (real e2e, 11-25, bug C): a confirmed uninstall cleans up, at once, every removed agent set up on this
    computer (its dead key's one confirm, then the plugin's fixed-path cleanup), not only the signer; an agent on another
    computer still cleans up at its own next start; and one whose files can't be removed here is named plainly."""

    def setUp(self):
        super().setUp()
        two_agents_elsewhere(self)

    def remove(self, text, targets, removed, not_confirmed):
        self.prompt("/datastash:uninstall")
        self.tool("PostToolUse", "ledger_agents_list", {}, LIST)
        self.prompt(text)
        got = self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(got["targets"], sorted(targets))
        status = "/api/v1/agent/uninstall/" + RUN
        self.answers[status] = (200, {"run_id": RUN, "kind": "uninstall", "total": len(targets), "confirmed": [],
                                      "not_confirmed": not_confirmed})
        return self.tool("PostToolUse", "ledger_agents_note", got, {
            "run_id": RUN, "kind": "uninstall", "targets": len(targets), "removed": removed, "this_agent_last": False,
            "status_path": status})["systemMessage"]

    def test_a_removed_agent_on_this_computer_goes_at_once_and_one_elsewhere_is_left_to_its_next_start(self):
        line = self.remove("web-app beta", [OWN, BETA], ["web-app", "beta"], ["web-app", "beta"])  # web-app: not here
        self.assertFalse(os.path.exists(ds.key_path("beta")))
        self.assertFalse(os.path.exists(ds.conf_path("beta")))
        self.assertIn(("stop", "beta"), self.calls)
        self.assertIn(("POST", ds.UNINSTALL_CONFIRM_PATH, KEY_B, {}), self.calls)
        self.assertTrue(os.path.exists(ds.key_path("alpha")))  # the signer, not named, stays
        self.assertIn("Still to clean up on their own computers when they next start: web-app.", line)
        self.assertNotIn("start: web-app and beta", line)
        self.assertNotIn("Not cleaned up", line)
        self.assertNotIn(KEY_B, line)

    def test_a_cleanup_that_cannot_finish_says_so(self):
        keep = ds.remove_agent_here
        ds.remove_agent_here = lambda s: False  # its files stay (a folder that is not writable)
        try:
            line = self.remove("beta", [BETA], ["beta"], ["beta"])
        finally:
            ds.remove_agent_here = keep
        self.assertIn("Not cleaned up on this computer: beta", line)
        self.assertTrue(line.startswith("Partly done. Datastash is uninstalled from beta."), line)
        self.assertTrue(os.path.exists(ds.key_path("beta")))

    def test_nothing_here_is_touched_when_the_server_refused(self):
        self.prompt("/datastash:uninstall")
        self.tool("PostToolUse", "ledger_agents_list", {}, LIST)
        self.prompt("beta")
        got = self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["updatedInput"]
        self.tool("PostToolUse", "ledger_agents_note", got, {"error": "braked", "code": "braked"})
        self.assertTrue(os.path.exists(ds.key_path("beta")))
        self.assertFalse(any(c[1] == ds.UNINSTALL_CONFIRM_PATH for c in self.calls if len(c) > 1))


class Commands(unittest.TestCase):
    def test_the_commands_are_the_persons_only_and_no_hook_starts_a_login(self):
        for n in ("install", "uninstall"):
            with open(os.path.join(HERE, "..", "plugins", "datastash", "commands", n + ".md")) as f:
                self.assertIn("\ndisable-model-invocation: true\n", f.read())
        self.assertFalse(ds.hooks_offer_sign_in())
        self.assertIsNone(ds.UNINSTALL_RE.fullmatch("please datastash uninstall"))
        self.assertEqual(ds.show_name("a\u202eb\x07c\n"), "abc")


class UninstallResultLines(Base):
    """1.0.16 (owner's Mac, 11-25): the uninstall's result is one plain line the model relays word for word, on the
    note call's own answer (never a denied call or a collapsed note alone): full removal, partial removal, and nothing
    installed here (the owner's line, never "did not answer")."""

    def setUp(self):
        super().setUp()
        self.installed()

    def remove(self, typed, targets, self_answer=(200, {"result": "removed", "run_id": RUN})):
        self.answers[ds.UNINSTALL_SELF_PATH] = self_answer
        self.prompt("/datastash:uninstall")
        self.tool("PostToolUse", "ledger_agents_list", {}, LIST)
        self.prompt(typed)
        pre = self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", pre)  # success is never a denied call
        return self.tool("PostToolUse", "ledger_agents_note", pre["updatedInput"], {
            "run_id": RUN, "kind": "uninstall", "targets": len(targets), "removed": [], "this_agent_last": True})

    def relayed(self, out, line):
        self.assertEqual(out["systemMessage"], line)
        hs = out["hookSpecificOutput"]
        self.assertEqual(hs["updatedMCPToolOutput"], [{"type": "text", "text": line}])
        self.assertIn("\n----\n" + line + "\n----", hs["additionalContext"])
        self.assertIn("word for word", hs["additionalContext"])
        for bad in ("denied", "error", "not run", "goes last", "will confirm"):
            self.assertNotIn(bad, line.lower())

    def test_full_removal(self):
        out = self.remove("web-app, alpha", [OWN, ALPHA])
        self.relayed(out, "Done. Datastash is uninstalled from web-app and alpha. Still to clean up on their own computers "
                          "when they next start: alpha. Your datastash connector in Claude is unchanged.")
        self.assertFalse(os.path.exists(ds.key_path("web-app")))

    def test_partial_removal_names_what_was_and_was_not_removed(self):
        out = self.remove("web-app, alpha", [OWN, ALPHA], self_answer=(429, {"code": "braked"}))
        self.relayed(out, "Partly done. Datastash is uninstalled from alpha. Not removed: web-app (too many removals just "
                          "now; wait a few minutes, then try again). Still to clean up on their own computers when they next start: "
                          "alpha. Your datastash connector in Claude is unchanged.")
        self.assertTrue(os.path.exists(ds.key_path("web-app")))

    def test_nothing_installed_says_so_and_never_did_not_answer(self):
        self.remove("this", [OWN])
        self.assertFalse(os.path.exists(ds.CONF))
        for text in ("/datastash:uninstall", "/datastash:uninstall all", "/datastash:uninstall web-app"):
            v, _ = self.prompt(text)  # the second run, same session
            self.assertEqual((v["decision"], v["reason"]), ("block", "Datastash isn't installed on this agent. Nothing "
                                                                      "was removed."))
        self.assertFalse(os.path.exists(ds.CONF))  # saying so makes no folder
        for f in ("uninstall.md",):
            with open(os.path.join(ROOT_PLUGIN, "commands", f)) as fh:
                body = fh.read()
            self.assertNotIn("did not answer", body)
            self.assertNotIn("only the plugin's own line says", body)
            self.assertIn("word for word", body)


class ReinstallKeepsTheName(Base):
    """1.0.16 (owner's Mac, 11-25): /datastash:install right after an uninstall in the same session brought back
    "agent", not the session's own agent's name. The uninstall leaves a non-secret record of the names it removed; the
    install reuses this session's (else this folder's) name, else the folder's or the computer's user name, else asks.
    Never "agent". The install's result is plain lines the model relays word for word."""

    def setUp(self):
        super().setUp()
        ds.save_agent("robin", OWN, KEY, WB, self.folder)
        c = ds.read_json(ds.conf_path("robin"), {})
        c["answer_store"] = ds.save_agent_answer("robin", ANSWER)
        ds.write_private(ds.conf_path("robin"), json.dumps(c))
        ds.bind_session_agent("s1", "robin", OWN)

    def uninstall(self):
        self.answers[ds.UNINSTALL_SELF_PATH] = (200, {"result": "removed", "run_id": RUN})
        lst = dict(LIST, agents=[{"agent_id": OWN, "name": "robin", "visibility": "public"}])
        self.prompt("/datastash:uninstall")
        self.tool("PostToolUse", "ledger_agents_list", {}, lst)
        self.prompt("robin")
        got = self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["updatedInput"]
        out = self.tool("PostToolUse", "ledger_agents_note", got, {"run_id": RUN, "kind": "uninstall", "targets": 1,
                                                                    "removed": [], "this_agent_last": True})
        self.assertTrue(out["systemMessage"].startswith("Done. Datastash is uninstalled from robin."))
        self.assertFalse(os.path.exists(ds.CONF))

    def test_same_session_reinstall_brings_the_same_name_back(self):
        self.uninstall()
        rec = ds.read_json(ds._last_names_path(), {})
        self.assertEqual(rec["names"][0]["name"], "robin")
        self.assertIn("s1", rec["names"][0]["sids"])
        self.assertEqual(stat.S_IMODE(os.stat(ds._last_names_path()).st_mode), 0o600)
        self.assertNotIn(KEY, json.dumps(rec))
        self.assertNotIn(ANSWER, json.dumps(rec))
        v, _ = self.prompt("/datastash:install")
        ai = v["hookSpecificOutput"]["additionalContext"]
        self.assertIn('ledger_agent_register with name "robin"', ai)
        self.assertNotIn('name "agent"', ai)
        self.assertNotIn('name "web-app"', ai)

    def test_a_new_session_in_the_same_folder_gets_the_name_too(self):
        self.uninstall()
        v, _ = self.prompt("/datastash:install", sid="s9")
        self.assertIn('ledger_agent_register with name "robin"', v["hookSpecificOutput"]["additionalContext"])

    def test_no_name_at_all_asks_and_a_typed_name_is_used(self):
        for f in (ds.conf_path("robin"),):
            ds._drop(f)
        ds._drop(ds.key_path("robin"))
        home_saved = ds.HOME
        self.addCleanup(setattr, ds, "HOME", home_saved)
        ds.HOME = self.folder  # the home folder: its own name is never the agent's
        with mock.patch.object(ds, "user_name", return_value=""), mock.patch.object(ds, "_folder_name", return_value=""):
            v, _ = self.prompt("/datastash:install", sid="s7")
            self.assertEqual((v["decision"], v["reason"]), ("block", ds._plain_typed(ds.ASK_NAME_LINE)))
            v, _ = self.prompt("/datastash:install my-agent", sid="s7")
            self.assertIn('ledger_agent_register with name "my-agent"', v["hookSpecificOutput"]["additionalContext"])
            v, _ = self.prompt("/datastash:install all", sid="s7")
            self.assertEqual(v["decision"], "block")  # a reserved word is never a name

    def test_the_install_result_is_plain_lines_relayed_word_for_word(self):
        self.prompt("/datastash:install")
        out = self.tool("PostToolUse", "ledger_agents_list", {}, dict(LIST, agents=[
            {"agent_id": OWN, "name": "robin", "visibility": "public"}]))
        text = out["hookSpecificOutput"]["updatedMCPToolOutput"][0]["text"]
        self.assertEqual(out["systemMessage"], ds._plain_typed(text))  # 1.0.18: the plain line shows no markdown
        self.assertTrue(text.startswith("Done. Datastash is on for robin. Time per card is on."), text)
        self.assertIn("\ndatastash is on for your agents:\n- robin (public)\n", text)
        self.assertEqual(out["hookSpecificOutput"]["updatedMCPToolOutput"], [{"type": "text", "text": text}])
        self.assertIn(text, out["hookSpecificOutput"]["additionalContext"])
        for bad in ("denied", "error", "not run", "will show"):
            self.assertNotIn(bad, text.lower())

    def test_waiting_items_are_named_one_line_each_with_how_to_answer(self):
        rec = {"name": "robin", "workbook_id": WB}
        with mock.patch.object(ds, "read_status_cache", return_value={"listening": {"waiting": [{"name": "beta"}]},
                                                                      "person_asks": []}), \
                mock.patch.object(ds, "_person_asks_of", return_value=[{"kind": "ask", "text": "Ship the fix?"}]):
            lines = ds.waiting_lines(rec)
        self.assertEqual(lines, ["- Not set up yet: beta. Type /datastash:install in its session.",
                                 "- Ship the fix? To answer, type **status**, then the number it shows."])


# 1.0.23 (card 1): this file's tests run the connector's install on a computer whose connector is signed in (a computer
# with no sign-in gets the email sign-in line instead: test/test_email_signin.py). Files that import Base keep the real check.
for _c in [v for v in list(globals().values()) if isinstance(v, type) and issubclass(v, Base) and v is not Base and v.__module__ == __name__]:
    _c.CONNECTOR_SIGNED_IN = True


if __name__ == "__main__":
    unittest.main()
