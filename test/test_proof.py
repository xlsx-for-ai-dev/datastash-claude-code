#!/usr/bin/env python3
"""Offline tests for XLI-351 (plugin 0.9.8): each agent's own proof on its datastash connector calls (the XLI-349 binding).
The server (src/agents/agent-proof.ts, withAgentCall) acts as a bound agent on a sign-in connection only when the call
carries that agent's proof in the `agent_proof` argument. The plugin already sends it on its own REST calls (register,
key) and uses the agent's own key elsewhere (listener, `datastash api`); this card is the connector path.
- PreToolUse on the plugin's own datastash connector (configured URL on getdatastash.com): a call whose agent_id is THIS
  session's agent (the folder's agent, as resolve says) gets that agent's proof (agent-<slug>.proof) as agent_proof,
  replacing any the AI sent. Any other call (another agent's agent_id, no agent_id, no agent here, no proof file, the
  person's main session) goes out with no agent_proof at all.
- Never on ledger_agent_register or ledger_agent_key: nothing the AI triggers mints or ties with the proof.
- Never to a server the plugin can't tell is datastash's (an unknown server, another URL).
- The proof is never printed except inside the hook's updatedInput, never logged, and a Bash or file tool call naming
  the proof files (agent-*.proof, enroll.json) is denied.
No network, no key; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_proof.py"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness  # noqa: E402,F401
from test_private import PrivateBase, OWN_ID, OTHER_ID, deny_of  # noqa: E402
from test_status import ds  # noqa: E402

PROOF = "Pq" * 21 + "z"  # 43 characters, secrets.token_urlsafe(32)'s shape
OTHER_PROOF = "Zy" * 21 + "a"
CONN = "mcp__plugin_datastash_datastash__"


def updated(out):
    return ((out or {}).get("hookSpecificOutput") or {}).get("updatedInput")


class ProofBase(PrivateBase):
    def setUp(self):
        super().setUp()
        ds.note_private({"agent_id": OWN_ID, "private": False}, OWN_ID)
        ds.write_private(ds.proof_path("web-app"), PROOF + "\n")
        self.other = self.other_folder("docs")
        ds.write_private(ds.conf_path("docs"), json.dumps({"name": "docs", "agent_id": OTHER_ID, "workbook_id": "wb2",
                                                           "projects": [self.other]}))
        ds.write_private(ds.key_path("docs"), "ldgr_ag_" + "d" * 40)
        ds.write_private(ds.proof_path("docs"), OTHER_PROOF + "\n")

    def call(self, tool, args, folder=None, event="PreToolUse"):
        if folder is not None:
            os.environ["CLAUDE_PROJECT_DIR"] = folder
        inp = {"session_id": "sess1", "hook_event_name": event, "tool_name": tool, "tool_input": args,
               "cwd": folder or self.folder}
        ds.hook_input = lambda: inp
        rc, out, err = self.run_cmd(ds.cmd_agent_tool, [])
        self.assertEqual(rc, 0, err)
        self.assertNotIn(PROOF, err)
        self.assertNotIn(OTHER_PROOF, err)
        return (json.loads(out) if out.strip() else None), out


class SendsOwnProof(ProofBase):
    def test_own_agent_id_gets_this_agents_proof(self):
        args = {"agent_id": OWN_ID, "workbook_id": "wb1", "rows": [{"a": 1}]}
        got, _raw = self.call(CONN + "ledger_rows_upsert", args)
        self.assertEqual(updated(got), dict(args, agent_proof=PROOF))
        self.assertEqual(args.get("agent_proof"), None)  # the input dict itself is not changed

    def test_agent_id_match_ignores_case_and_spaces(self):
        got, _ = self.call(CONN + "ledger_rows_get", {"agent_id": " " + OWN_ID.upper() + " "})
        self.assertEqual(updated(got)["agent_proof"], PROOF)

    def test_the_ais_own_agent_proof_is_replaced(self):
        got, _ = self.call(CONN + "ledger_rows_get", {"agent_id": OWN_ID, "agent_proof": OTHER_PROOF})
        self.assertEqual(updated(got), {"agent_id": OWN_ID, "agent_proof": PROOF})

    def test_the_other_folders_session_sends_its_own_proof(self):
        got, _ = self.call(CONN + "ledger_rows_get", {"agent_id": OTHER_ID}, folder=self.other)
        self.assertEqual(updated(got)["agent_proof"], OTHER_PROOF)


class SendsNoOtherProof(ProofBase):
    def assert_no_proof(self, got, args):
        u = updated(got)
        if "agent_proof" in args:
            self.assertEqual(u, {k: v for k, v in args.items() if k != "agent_proof"})
        else:
            self.assertTrue(u is None or "agent_proof" not in u, u)

    def test_another_agents_agent_id_gets_no_proof(self):
        for args in ({"agent_id": OTHER_ID}, {"agent_id": OTHER_ID, "agent_proof": PROOF},
                     {"agent_id": OTHER_ID, "agent_proof": OTHER_PROOF}):
            got, raw = self.call(CONN + "ledger_rows_get", args)
            self.assert_no_proof(got, args)
            self.assertNotIn(PROOF, raw)
            self.assertNotIn(OTHER_PROOF, raw)

    def test_a_session_naming_the_first_agent_from_another_folder_gets_no_proof(self):
        got, raw = self.call(CONN + "ledger_rows_get", {"agent_id": OWN_ID}, folder=self.other)
        self.assert_no_proof(got, {"agent_id": OWN_ID})
        self.assertNotIn(PROOF, raw)

    def test_no_agent_id_gets_no_proof(self):
        for args in ({}, {"workbook_id": "wb1"}, {"agent_proof": PROOF}, {"agent_id": ""}, {"agent_id": 7}):
            got, raw = self.call(CONN + "ledger_workbook_list", args)
            self.assert_no_proof(got, args)
            self.assertNotIn(PROOF, raw)

    def test_register_and_key_never_carry_the_proof(self):
        for tool, args in (("ledger_agent_register", {"name": "web-app", "agent_id": OWN_ID}),
                           ("ledger_agent_register", {"name": "web-app", "agent_id": OWN_ID, "agent_proof": PROOF}),
                           ("ledger_agent_key", {"name": "web-app", "agent_id": OWN_ID}),
                           ("ledger_agent_key", {"name": "web-app", "agent_id": OWN_ID, "agent_proof": PROOF})):
            got, raw = self.call(CONN + tool, args)
            self.assertNotIn(PROOF, raw, tool)
            u = updated(got)
            self.assertTrue(u is None or "agent_proof" not in u, (tool, u))

    def test_no_proof_file_strips_only(self):
        os.replace(ds.proof_path("web-app"), ds.proof_path("web-app") + ".moved")
        got, _ = self.call(CONN + "ledger_rows_get", {"agent_id": OWN_ID})
        self.assertIsNone(updated(got))  # an old install: unbound, works as before
        got, _ = self.call(CONN + "ledger_rows_get", {"agent_id": OWN_ID, "agent_proof": OTHER_PROOF})
        self.assertEqual(updated(got), {"agent_id": OWN_ID})

    def test_a_proof_file_others_can_read_is_not_used(self):
        os.chmod(ds.proof_path("web-app"), 0o644)
        got, raw = self.call(CONN + "ledger_rows_get", {"agent_id": OWN_ID})
        self.assertNotIn(PROOF, raw)

    def test_a_folder_with_no_agent_gets_no_proof(self):
        bare = self.other_folder("bare")
        got, raw = self.call(CONN + "ledger_rows_get", {"agent_id": OWN_ID, "agent_proof": PROOF}, folder=bare)
        self.assertEqual(updated(got), {"agent_id": OWN_ID})
        self.assertNotIn(PROOF, raw)

    def test_the_persons_main_session_gets_no_proof(self):
        ds.is_main_session = lambda me: True
        self.addCleanup(setattr, ds, "is_main_session", self.saved_main)
        got, raw = self.call(CONN + "ledger_rows_get", {"agent_id": OWN_ID})
        self.assertNotIn(PROOF, raw)

    def setUp(self):
        super().setUp()
        self.saved_main = ds.is_main_session

    def test_servers_that_are_not_datastash_never_get_it(self):
        for tool in ("mcp__datastash__ledger_rows_get", "mcp__claude_ai_datastash__ledger_rows_get",
                     "mcp__other__ledger_rows_get", "mcp__evil_datastash_copy__ledger_rows_get"):
            got, raw = self.call(tool, {"agent_id": OWN_ID})
            self.assertNotIn(PROOF, raw, tool)

    def test_a_datastash_named_server_drops_the_ais_agent_proof(self):
        got, raw = self.call("mcp__datastash__ledger_rows_get", {"agent_id": OWN_ID, "agent_proof": PROOF})
        self.assertEqual(updated(got), {"agent_id": OWN_ID})

    def test_post_tool_use_prints_no_proof(self):
        for event in ("PostToolUse", "PostToolUseFailure"):
            got, raw = self.call(CONN + "ledger_rows_get", {"agent_id": OWN_ID, "agent_proof": PROOF}, event=event)
            self.assertNotIn(PROOF, raw, event)

    def test_never_in_a_log_or_state_file(self):
        self.call(CONN + "ledger_rows_get", {"agent_id": OWN_ID})
        self.call(CONN + "ledger_rows_get", {"agent_id": OTHER_ID, "agent_proof": PROOF})
        for d in (ds.STATE,):
            for root, _dirs, names in os.walk(d):
                for n in names:
                    p = os.path.join(root, n)
                    try:
                        with open(p, errors="replace") as f:
                            self.assertNotIn(PROOF, f.read(), p)
                    except OSError:
                        pass


class ProofFilesDenied(ProofBase):
    def test_bash_and_file_tools_naming_the_proof_files_are_denied(self):
        p = ds.proof_path("web-app")
        for tool, args in (("Bash", {"command": f"cat {p}"}),
                           ("Bash", {"command": "cat ~/.config/datastash/agent-*.proof"}),
                           ("Bash", {"command": "cat ~/.config/datastash/enroll.json"}),
                           ("Bash", {"command": f"cat {ds.ENROLL_FILE}"}),
                           ("Read", {"file_path": p}),
                           ("Read", {"file_path": ds.ENROLL_FILE}),
                           ("Grep", {"pattern": ".", "path": p}),
                           ("Glob", {"pattern": "**/agent-*.proof"})):
            got, raw = self.call(tool, args)
            self.assertEqual(deny_of(got), "deny", (tool, args))
            self.assertNotIn(PROOF, raw)

    def test_unrelated_files_are_not_denied(self):
        for tool, args in (("Bash", {"command": "cat notes/math.proof"}), ("Read", {"file_path": "/tmp/enroll.json"}),
                           ("Bash", {"command": "ls"})):
            got, _ = self.call(tool, args)
            self.assertNotEqual(deny_of(got), "deny", (tool, args))


if __name__ == "__main__":
    unittest.main()
