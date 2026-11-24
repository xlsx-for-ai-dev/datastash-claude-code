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
import stat
import subprocess
import sys
import time
import unittest
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
KEY, ANSWER = "ldgr_ag_" + "k" * 40, "A" * 43
LIST = {"list_id": LIST_ID, "expires_in_seconds": 600,
        "agents": [{"agent_id": OWN, "name": "web-app", "visibility": "public"},
                   {"agent_id": ALPHA, "name": "alpha‮", "visibility": "private"},
                   {"agent_id": BETA, "name": "beta", "visibility": "public"}]}
TOOL = "mcp__claude_ai_datastash__"
OWN_TOOL = "mcp__plugin_datastash_datastash__"  # the plugin's own connector (https://getdatastash.com/mcp): trusted by URL
PATCHED = ("CONF", "STATE", "PENDING", "OFF", "MAIN_DIR", "http", "_claude_chain", "_kc", "_stop_listener", "_gone_path", "hook_input",
           "spawn_detached")


def real_entries(name):
    """The real Claude Code 2.1.330 transcript entries of one typed slash command (its own entry and its isMeta
    companion), from the fixture."""
    with open(os.path.join(FIXTURES, "slash-command-2.1.330.jsonl")) as f:
        rows = [json.loads(x) for x in f if x.strip()]
    own = [r for r in rows if isinstance(r["message"]["content"], str) and f"<command-name>/{name}</command-name>" in r["message"]["content"]][0]
    return own, [r for r in rows if r["promptId"] == own["promptId"] and r is not own][0]


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {k: getattr(ds, k) for k in PATCHED}
        root = os.path.realpath(harness.tmpdir(self, prefix="install-", dir=harness.ROOT))
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


def proof_ok(p, kind, targets, pid, typed, own=OWN):
    """X2's checkNoteProof, line for line: exactly these 7 fields, and the MAC over its 9-line proofInput."""
    if sorted(p) != ["agent_id", "iat", "mac", "prompt_id", "typed_at", "typed_sha256", "v"] or p["v"] != 1:
        return False
    if p["agent_id"] != own or p["prompt_id"] != pid or not 0 <= p["iat"] - p["typed_at"] <= 125000:
        return False
    lines = ["datastash-agents-note/v1", own, LIST_ID.lower(), kind, ",".join(sorted(t.lower() for t in targets)), pid,
             hashlib.sha256(typed.encode()).hexdigest(), str(p["typed_at"]), str(p["iat"])]
    return hmac.compare_digest(p["mac"], hmac.new(ANSWER.encode(), "\n".join(lines).encode(), hashlib.sha256).hexdigest())


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
        self.assertEqual(done, "datastash is on for your agents:\n- web-app (public)\n- alpha (private)\n- beta (public)\n"
                               "To remove one or many: /datastash:uninstall and we'll show you the list of agents.\n"
                               "To make one private: say make <name> private.\n"
                               "To make one public: say make <name> public.\n"
                               "Time per card is on. To turn it off, type: datastash time-per-card off")

    def test_the_only_agent_gets_the_list_with_no_note(self):
        self.installed()
        self.slash("/datastash:install")
        out = self.tool("PostToolUse", "ledger_agents_list", {}, dict(LIST, agents=LIST["agents"][:1]))
        self.assertEqual(out["systemMessage"], "datastash is on for your agents:\n- web-app (public)\n" + ds.REMOVE_LINE
                         + "\n" + ds.PRIVATE_LINE + "\n" + ds.PUBLIC_LINE + "\n" + ds.TPC_ON_LINE)
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
        self.assertEqual(out["systemMessage"], ds.INSTALL_FAILED_LINE)
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
        self.assertEqual(out["decision"], "block")
        self.assertIn("ledger_agent_key now", out["reason"])
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
                                + ds.PICK_LINE)
        v, pid = self.prompt("Beta")
        self.assertIn('kind "uninstall"', v["hookSpecificOutput"]["additionalContext"])
        got = self.tool("PreToolUse", "ledger_agents_note", {"targets": [OWN, ALPHA]})["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(got["targets"], [BETA])  # never the model's targets
        self.assertTrue(proof_ok(got["typed_proof"], "uninstall", [BETA], pid, "Beta"))
        self.assertEqual(sorted(got), ["kind", "list_id", "targets", "typed_proof"])
        again = self.tool("PreToolUse", "ledger_agents_note", {})  # used once
        self.assertEqual(again["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_all_in_one_go_removes_this_agent_last_and_says_who_is_unconfirmed(self):
        v, pid = self.slash("/datastash:uninstall all")  # the real slash-command entry, with args
        self.assertIsNone(v.get("decision"))
        self.tool("PostToolUse", "ledger_agents_list", {}, LIST)
        got = self.tool("PreToolUse", "ledger_agents_note", {})["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(got["targets"], sorted([OWN, ALPHA, BETA]))
        self.assertTrue(proof_ok(got["typed_proof"], "uninstall", [OWN, ALPHA, BETA], pid, "/datastash:uninstall all"))
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
        lst = dict(LIST, agents=LIST["agents"] + [{"agent_id": str(uuid.UUID(int=4)), "name": "Be ta", "visibility": "public"}])
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
        self.assertIn(("POST", ds.UNINSTALL_SELF_PATH, KEY, {}), self.calls)


class RealSlashShape(Base):
    """Review 1 HIGH 1: the real Claude Code 2.1.330 entry of a typed slash command passes; nothing else like it does."""

    def setUp(self):
        super().setUp()
        self.installed()

    def note_after(self, **change):
        self.slash("/datastash:uninstall all", **change)
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

    def test_the_older_tag_order_and_a_name_that_differs_are_refused(self):
        for content in ("<command-name>/datastash:uninstall</command-name>\n<command-message>datastash:uninstall"
                        "</command-message>\n<command-args>all</command-args>",
                        "<command-message>datastash:install</command-message>\n<command-name>/datastash:uninstall"
                        "</command-name>\n<command-args>all</command-args>"):
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
        out = self.tool("PreToolUse", "ledger_agents_note", {})
        self.assertIn('datastash is off for "web-app"', out["systemMessage"])
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
        self.install("s1", register="agent")
        self.go("web-app")
        self.install("s2", register="web-app", own=ALPHA)  # not the home agent: this folder gets its own
        self.assertEqual(self.agents(), {"agent": [], "web-app": ["web-app"]})
        self.install("s2", own=ALPHA, real=REAL_NOTE_2)  # a retry in the same session: the same agent, no -2
        self.go("web-app")
        self.install("s3", own=ALPHA)  # a later session in the folder: its linked agent
        self.go("home")
        self.install("s4")  # a later session in the home folder: the home agent (the one with no folder)
        self.assertEqual(self.agents(), {"agent": [], "web-app": ["web-app"]})

    def test_folder_install_then_home_makes_a_home_agent(self):
        self.go("web-app")
        self.install("s1", register="web-app")
        self.go("home")
        self.install("s2", register="agent", own=ALPHA)  # the folder's agent is never the home folder's
        self.install("s2", own=ALPHA, real=REAL_NOTE_2)  # retry, same session
        self.install("s3", own=ALPHA)  # a new session in home: the one agent with no folder
        self.assertEqual(self.agents(), {"agent": [], "web-app": ["web-app"]})

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
        self.install("s1", register="agent")
        self.go("web-app")
        self.install("s2", register="web-app", own=ALPHA)
        self.go("api")
        self.install("s3", register="api", own=BETA)
        self.assertEqual(self.agents(), {"agent": [], "web-app": ["web-app"], "api": ["api"]})

    def test_no_folder_fallback_in_a_folder_it_can_link(self):
        self.install("s1", register="agent")
        self.go("web-app")
        inp = {"session_id": "s2", "cwd": self.folder, "hook_event_name": "PreToolUse"}
        self.assertIsNone(ds.session_agent(inp))
        self.assertIsNone(ds._own(inp))
        self.go("home")
        self.assertEqual(ds.session_agent(dict(inp, cwd=self.folder))["name"], "agent")  # home: the fallback holds

    def test_uninstall_drops_the_session_record(self):
        self.install("s1", register="agent")
        self.go("web-app")
        self.install("s2", register="web-app", own=ALPHA)
        rec = lambda sid: os.path.exists(os.path.join(ds.STATE, f"session-agent-{sid}.json"))
        self.assertTrue(rec("s1") and rec("s2"))
        self.assertFalse(ds.remove_agent_here("agent"))  # web-app is left
        self.assertFalse(rec("s1"))
        self.assertTrue(rec("s2"))
        self.assertTrue(ds.remove_agent_here("web-app"))
        self.assertFalse(rec("s2"))

    def test_fresh_home_folder_install_sends_the_note_and_a_retry_reuses_the_agent(self):
        v, pid = self.slash("/datastash:install")
        ai = v["hookSpecificOutput"]["additionalContext"]
        self.assertIn('ledger_agent_register with name "agent"', ai)  # not the computer's user (the home folder's name)
        self.assertNotIn("home-user", ai)
        self.mint("agent")
        self.list_and_note(REAL_NOTE_1, pid)
        # The retry, in the same session: no new agent, and the note goes again (the model's call 2 shape: no list id).
        v2, pid2 = self.slash("/datastash:install")
        ai2 = v2["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("ledger_agent_register", ai2)
        self.assertNotIn("agent-2", ai2)
        self.list_and_note(REAL_NOTE_2, pid2)
        self.assertEqual([a["name"] for a in ds.all_agents()], ["agent"])

    def test_the_session_agent_name_wins_over_the_folder(self):
        with open(os.path.join(FIXTURES, "slash-command-2.1.330-hooks.json")) as f:
            shape = [h for h in json.load(f) if h["prompt"] == "/datastash:install"][0]
        v = self.hook(ds.cmd_prompt, dict(shape, session_id="s1", cwd=self.folder, prompt_id=str(uuid.uuid4()),
                                          transcript_path=self.transcript("s1"), agent_type="SPM"))
        self.assertIn('ledger_agent_register with name "SPM"', v["hookSpecificOutput"]["additionalContext"])

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
        self.assertEqual(out["systemMessage"], ds.NO_OWN_AGENT_LINE)
        _, pid = self.slash("/datastash:install")
        self.mint("agent")
        got = self.list_and_note(REAL_NOTE_1, pid)
        out = self.tool("PostToolUse", "ledger_agents_note", got, {"code": "not_typed", "message": "Only the person's own "
                        "typed answer, through the datastash plugin, sends agent notes. Nothing changed."})
        self.assertEqual(out["systemMessage"], "Not done: datastash refused it (not_typed: Only the person's own typed "
                         "answer, through the datastash plugin, sends agent notes). Nothing changed.")
        self.assertEqual(out["systemMessage"].count("\n"), 0)

    def test_the_connector_line_is_the_mcp_authenticate_step(self):
        self.assertEqual(ds.NO_CONNECTOR_LINE, "Type /mcp, pick datastash, choose Authenticate, then run /datastash:install again.")
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
        self.assertEqual(done["systemMessage"].split("\n")[:4], [ds.ON_HEAD, "- web-app (public)", "- alpha (private)",
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
