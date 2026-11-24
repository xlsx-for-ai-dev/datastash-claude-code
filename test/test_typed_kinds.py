#!/usr/bin/env python3
"""Offline tests for XLI-388 (plugin 1.0.10): the person's typed answers to datastash's own asks and their typed undo of a
default an agent took, numbered and signed exactly like a plain ask's typed answer (XLI-310 item 2), as x-live #493
checks them:
- the person's status read (GET /api/v1/person/status) carries `handoffs` (a stalled one with its `ask`), `stuck`
  (circle, wait, chatter) and `defaults`; the plugin keeps them in the local copy (an agent key's read keeps the last
  ones) and numbers them after the agents in the list it shows itself;
- "N: <agent name>" on a hand-off ask, "N: <option>" (or the person's own words, circle and chatter only) on a stuck ask:
  proved typed by the transcript checker, then the plugin itself posts {workbook_id, instance, answer, typed_proof} to
  /api/v1/person/status/answer, the MAC under "datastash-typed-answer/v1" over that instance and that answer;
- "undo N" on a default taken: posted to /api/v1/person/status/default/undo as {workbook_id, instance, typed_proof},
  the MAC under "datastash-typed-undo-default/v1" over the fixed answer "undo_default";
- nothing model-triggered makes a proof (a hook-shaped prompt piped by a tool call, a headless or system prompt, a
  reused prompt id, a pending record whose prompt names another number or kind), and a proof never names another
  kind or ask.
No network, no key; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_typed_kinds.py"""
import hashlib
import hmac
import json
import os
import sys
import time
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness  # noqa: E402,F401
import test_status  # noqa: E402
from test_status import Base, ds, row, KEY, GRANT, WB  # noqa: E402

I_HANDOFF = "aaaaaaaa-0000-4000-8000-000000000001"
I_CIRCLE = "aaaaaaaa-0000-4000-8000-000000000002"
I_WAIT = "aaaaaaaa-0000-4000-8000-000000000003"
I_CHATTER = "aaaaaaaa-0000-4000-8000-000000000004"
I_DEFAULT = "aaaaaaaa-0000-4000-8000-000000000005"
I_PLAIN = "aaaaaaaa-0000-4000-8000-000000000006"
UNDO_LABEL = "datastash-typed-undo-default/v1"
ANSWER_LABEL = "datastash-typed-answer/v1"


def person_body():
    """The extra blocks of a person status read, shaped as x-live #493's handoffBlock, stuckBlock and defaultsBlock."""
    return {
        "handoffs": [
            {"handoff_id": "h-1", "from": "Alpha", "to": "Beta", "project": "api", "state": "sent",
             "sent": {"task": "Review the open card", "written_by": "Alpha", "read_as": "x"},
             "ask": {"instance": I_HANDOFF, "kind": "handoff", "options": ["Beta", "Gamma"], "asked_at": "2026-11-24T00:00:00Z",
                     "text": 'Alpha handed "Review the open card" to Beta; not picked up. Who should take it?'}},
            {"handoff_id": "h-2", "from": "Alpha", "to": "Gamma", "project": "api", "state": "picked_up",
             "sent": {"task": "x", "written_by": "Alpha", "read_as": "x"}},  # no ask: not numbered
        ],
        "stuck": [
            {"instance": I_CIRCLE, "kind": "circle", "text": "Alpha and Beta are waiting on each other. Who goes first?",
             "options": ["Alpha", "Beta"], "agents": [{"name": "Alpha"}, {"name": "Beta"}], "asked_at": "x"},
            {"instance": I_WAIT, "kind": "wait", "text": "Gamma has waited 3 hours on Delta.",
             "options": ["keep waiting", "stop waiting"], "agents": [{"name": "Gamma"}, {"name": "Delta"}]},
            {"instance": I_CHATTER, "kind": "chatter", "text": "Eps and Zeta went back and forth 40 times.",
             "options": ["continue", "stop"], "agents": [{"name": "Eps"}, {"name": "Zeta"}], "held": 2},
        ],
        "defaults": [
            {"instance": I_DEFAULT, "agent": "Alpha", "project": "api", "taken_at": "2026-11-24T00:00:00.000Z",
             "ask": {"id": "a1", "text": "One-day or seven-day sessions?", "default": "one day", "by_when": "x",
                     "written_by": "Alpha", "read_as": "x"}},
        ],
    }


def mac(label, key, grant, wb, instance, answer, p):
    """The server's MAC (src/agents/answers.ts checkTypedProof at x-live 9d428c53), written here independently."""
    js = json.dumps(answer, ensure_ascii=False, separators=(",", ":"))
    lines = [label, grant.lower(), wb.lower(), instance.lower(), hashlib.sha256(js.encode()).hexdigest(), p["prompt_id"],
             p["typed_sha256"], str(p["typed_at"]), str(p["iat"])]
    return hmac.new(key.encode(), "\n".join(lines).encode(), "sha256").hexdigest()


class TypedKinds(Base):
    def setUp(self):
        super().setUp()
        self.server_rows = [row("api", "Waiting on the person.", project="api",
                                asks=[{"id": "p1", "text": "Ship the fix today?", "instance": I_PLAIN}])]
        self.extra = person_body()
        self.key = (KEY, GRANT)
        self.saved_k = (ds.read_answer_key, ds._open_browser, ds.answer_key_kept)
        ds.read_answer_key = lambda: self.key
        ds.answer_key_kept = lambda: self.key is not None
        ds._open_browser = lambda url: True
        self.addCleanup(self.restore_k)
        self.reply = (200, {"ok": True})
        base = ds.http

        def http(method, path, key=None, body=None, form=None, timeout=40):
            if path in (ds.PERSON_ANSWER_PATH, ds.PERSON_DEFAULT_UNDO_PATH):
                self.calls.append((method, path, key, body))
                status, out = self.reply
                if status >= 400:
                    raise ds.ApiError(status, out)
                return status, "application/json", json.dumps(out)
            st, ct, text = base(method, path, key, body, form, timeout)
            if method == "GET" and path.startswith(ds.PERSON_STATUS_PATH + "?"):
                b = json.loads(text)
                b.update(json.loads(json.dumps(self.extra)))
                text = json.dumps(b)
            return st, ct, text
        ds.http = http
        ds.fetch_status({"slug": ds.PERSON_SLUG, "workbook_id": WB})
        ds.write_private(ds.INSTALL_ASKED_PATH, "{}")
        self.shown = self.prompt("status of all my agents")
        self.calls.clear()

    def restore_k(self):
        ds.read_answer_key, ds._open_browser, ds.answer_key_kept = self.saved_k

    def numbers(self):
        return ds.read_view("sess1")["numbers"]

    def n_of(self, instance):
        [n] = [int(k) for k, v in self.numbers().items() if isinstance(v, dict) and v.get("instance") == instance]
        return n

    def sent(self):
        return [c for c in self.calls if c[1] in (ds.PERSON_ANSWER_PATH, ds.PERSON_DEFAULT_UNDO_PATH)]

    def check_proof(self, body, label, instance, answer, typed):
        p = body["typed_proof"]
        self.assertEqual(set(p), {"v", "prompt_id", "typed_sha256", "typed_at", "iat", "mac"})
        self.assertEqual(p["typed_sha256"], hashlib.sha256(typed.encode()).hexdigest())
        self.assertTrue(0 <= p["iat"] - p["typed_at"] <= 125_000)
        self.assertLessEqual(abs(time.time() * 1000 - p["iat"]), 60_000)
        self.assertEqual(p["mac"], mac(label, KEY, GRANT, WB, instance, answer, p))
        for other in {ANSWER_LABEL, UNDO_LABEL} - {label}:  # never valid as the other kind
            self.assertNotEqual(p["mac"], mac(other, KEY, GRANT, WB, instance, answer, p))
            self.assertNotEqual(p["mac"], mac(other, KEY, GRANT, WB, instance, "undo_default", p))

    # --- the list

    def test_the_list_numbers_each_ask_and_default_after_the_agents(self):
        nums = self.numbers()
        self.assertEqual(nums["2"]["ask"], "p1")  # the agent's own plain ask keeps its place
        kinds = [(v.get("person_ask"), v.get("instance")) for k, v in sorted(nums.items(), key=lambda kv: int(kv[0]))
                 if v.get("person_ask")]
        self.assertEqual(kinds, [("handoff", I_HANDOFF), ("stuck", I_CIRCLE), ("stuck", I_WAIT), ("stuck", I_CHATTER),
                                 ("default", I_DEFAULT)])
        self.assertIn("Waiting for you (from datastash):", self.shown)
        self.assertIn(f'{self.n_of(I_DEFAULT)}. Default taken by', self.shown)
        self.assertIn(f'Type "undo {self.n_of(I_DEFAULT)}"', self.shown)
        self.assertIn(f'Type "{self.n_of(I_HANDOFF)}: <one of: Beta, Gamma>"', self.shown)
        self.assertIn("or your own words", self.shown)
        self.assertEqual(self.calls, [])  # the plugin's own answer: from the local copy

    def test_an_agent_keys_read_keeps_the_persons_asks_and_the_persons_next_read_replaces_them(self):
        ds.fetch_status({"slug": "web-app", "workbook_id": WB})  # an agent key: no person blocks in its reply
        self.assertEqual(len(ds._person_asks_of(ds.read_status_cache(WB))), 5)
        self.extra = {"stuck": []}
        ds.fetch_status({"slug": ds.PERSON_SLUG, "workbook_id": WB})
        self.assertEqual(ds._person_asks_of(ds.read_status_cache(WB)), [])

    def test_bad_blocks_are_dropped(self):
        self.extra = {"handoffs": [{"from": "A", "to": "B", "ask": {"kind": "handoff", "instance": "not-a-uuid", "text": "t",
                                                                   "options": ["B"]}}],
                      "stuck": [{"instance": I_WAIT, "kind": "other", "text": "t", "options": ["x"]},
                                {"instance": I_CIRCLE, "kind": "circle", "text": "t", "options": ["A\nB"]}],
                      "defaults": [{"instance": I_DEFAULT, "agent": "A", "ask": {}}]}
        ds.fetch_status({"slug": ds.PERSON_SLUG, "workbook_id": WB})
        self.assertEqual(ds._person_asks_of(ds.read_status_cache(WB)), [])

    def test_full_context_of_a_person_ask_shows_its_line(self):
        n = self.n_of(I_WAIT)
        shown = self.prompt(f"full context of {n}")
        self.assertIn("Gamma has waited 3 hours on Delta.", shown)
        self.assertIn("keep waiting, stop waiting", shown)

    # --- hand-off

    def test_a_typed_hand_off_answer_is_signed_for_that_instance_and_name(self):
        n = self.n_of(I_HANDOFF)
        note = self.prompt(f"{n}: gamma")
        self.assertIn("never call", note.lower())
        [(method, path, key, body)] = self.sent()
        self.assertEqual((method, path, key), ("POST", ds.PERSON_ANSWER_PATH, "dsat_person"))
        self.assertEqual(body, {"workbook_id": WB, "instance": I_HANDOFF, "answer": "Gamma", "typed_proof": body["typed_proof"]})
        self.check_proof(body, ANSWER_LABEL, I_HANDOFF, "Gamma", f"{n}: gamma")
        self.assertEqual(self.outcomes(), ['Sent "Gamma" to Alpha.'])

    def test_a_hand_off_takes_the_receiver_too_and_never_do_n(self):
        self.extra["handoffs"][0]["ask"]["options"] = ["Gamma"]  # the receiver is off: not a taker, still the receiver
        ds.fetch_status({"slug": ds.PERSON_SLUG, "workbook_id": WB})
        self.prompt("status of all my agents")
        n = self.n_of(I_HANDOFF)
        for typed in (f"do {n}", f"no to {n}", f"{n}: Delta"):
            shown = self.prompt(typed)
            self.assertEqual(self.last_hook["decision"], "block", typed)
            self.assertIn("Nothing was sent", shown)
        self.prompt("status of all my agents")
        self.prompt(f"{n}: Beta")
        self.assertEqual([c[3]["answer"] for c in self.sent()], ["Beta"])

    # --- stuck

    def test_stuck_asks_take_an_option_or_words_where_the_server_does(self):
        n = self.n_of(I_WAIT)
        self.prompt(f"{n}: Keep Waiting")
        self.prompt("status of all my agents")
        shown = self.prompt(f"{n}: wait a bit more")  # a wait ask takes its options only
        self.assertIn("Nothing was sent", shown)
        self.prompt("status of all my agents")
        self.prompt(f"{self.n_of(I_CIRCLE)}: beta")
        self.prompt("status of all my agents")
        self.prompt(f"{self.n_of(I_CIRCLE)}:  Beta first, then Alpha  ")  # circle: the person's own words
        self.prompt("status of all my agents")
        self.prompt(f"{self.n_of(I_CHATTER)}: stop")
        self.prompt("status of all my agents")
        shown = self.prompt(f"do {self.n_of(I_CHATTER)}")  # "do N" is never words
        self.assertIn("or your own words", shown)
        got = [(c[3]["instance"], c[3]["answer"]) for c in self.sent()]
        self.assertEqual(got, [(I_WAIT, "keep waiting"), (I_CIRCLE, "Beta"), (I_CIRCLE, "Beta first, then Alpha"),
                               (I_CHATTER, "stop")])
        for c in self.sent():
            self.assertEqual(c[1], ds.PERSON_ANSWER_PATH)
            p = c[3]["typed_proof"]
            self.assertEqual(p["mac"], mac(ANSWER_LABEL, KEY, GRANT, WB, c[3]["instance"], c[3]["answer"], p))

    # --- undo a default

    def test_undo_n_is_signed_under_its_own_label_and_sent_to_the_undo_route(self):
        n = self.n_of(I_DEFAULT)
        note = self.prompt(f"Undo {n}.")
        self.assertIn("undo_default", note)
        [(method, path, key, body)] = self.sent()
        self.assertEqual((method, path, key), ("POST", ds.PERSON_DEFAULT_UNDO_PATH, "dsat_person"))
        self.assertEqual(set(body), {"workbook_id", "instance", "typed_proof"})
        self.assertEqual((body["workbook_id"], body["instance"]), (WB, I_DEFAULT))
        self.check_proof(body, UNDO_LABEL, I_DEFAULT, "undo_default", f"Undo {n}.")
        self.assertEqual(self.outcomes(), ["Sent: Alpha will undo the default it took."])

    def test_other_words_on_a_default_and_undo_on_anything_else_send_nothing(self):
        d = self.n_of(I_DEFAULT)
        for typed in (f"do {d}", f"{d}: undo", f"no to {d}"):
            self.prompt("status of all my agents")
            shown = self.prompt(typed)
            self.assertEqual(shown, ds.UNDO_ONLY_LINE.format(n=d, agent="Alpha"))
        for inst in (I_HANDOFF, I_CIRCLE):
            self.prompt("status of all my agents")
            self.assertIsNone(self.prompt(f"undo {self.n_of(inst)}"))  # an ordinary prompt
        self.prompt("status of all my agents")
        self.assertIsNone(self.prompt("undo 2"))  # the agent's own plain ask: no undo from here
        self.assertEqual(self.sent(), [])

    def test_held_already_undone_and_gone(self):
        n = self.n_of(I_DEFAULT)
        url = ds.ORIGIN + "/confirm/abc"
        self.reply = (403, {"code": "person_approval_needed", "approve_url": url})
        self.prompt(f"undo {n}")
        self.assertEqual(self.outcomes(), [f"Alpha's ask needs your OK on the datastash site: {url}"])
        for reply in ((409, {"code": "already_undone"}), (404, {"code": "not_found"})):
            self.reply = reply
            self.prompt("status of all my agents")
            self.prompt(f"undo {n}")
            self.assertEqual(self.outcomes(), [ds.ANSWER_CHANGED_LINE])

    # --- nothing model-triggered makes a proof

    def test_forged_headless_system_and_reused_prompts_send_nothing(self):
        for n, typed in ((self.n_of(I_DEFAULT), "undo {n}"), (self.n_of(I_HANDOFF), "{n}: Gamma")):
            t = typed.format(n=n)
            self.prompt("status of all my agents")
            self.outcomes()
            self.prompt(t, writer=self.headless)
            self.assertEqual(self.outcomes(), [ds.NOT_PERSON_LINE])
            self.prompt(t, origin={"kind": "system"})
            self.prompt(t, origin=False, source=False)
            pid = str(uuid.uuid4())
            self.forged({"session_id": "sess1", "prompt": t, "hook_event_name": "UserPromptSubmit", "cwd": self.folder,
                         "prompt_id": pid, "transcript_path": self.transcript("sess1")})
        self.assertEqual(self.sent(), [])

    def test_a_pending_record_must_match_the_typed_prompt(self):
        """Even a pending record written beside the person's real typed entry signs nothing when the typed words name
        another number or kind: an answer prompt never yields an undo proof, nor an undo prompt an answer proof."""
        n_d, n_h = self.n_of(I_DEFAULT), self.n_of(I_HANDOFF)
        cases = [("undo_default", f"{n_h}: Gamma", {"n": n_h}),  # typed an answer: no undo proof
                 ("answer", f"undo {n_d}", {"n": n_d, "answer": "undo_default"}),  # typed an undo: no answer proof
                 ("undo_default", f"undo {n_h}", {"n": n_d}),  # another number
                 ("answer", f"{n_h}: Gamma", {"n": n_h, "answer": "Beta"}),  # another answer
                 ("answer", f"{n_h}: Gamma", {"n": True, "answer": "Gamma"})]
        for kind, typed, data in cases:
            pid = str(uuid.uuid4())
            self.typed("sess1", typed, pid)
            rec = {"kind": kind, "sid": "sess1", "tp": self.transcript("sess1"), "pid": pid, "prompt": typed, "at": time.time(),
                   "data": dict({"workbook": WB, "instance": I_DEFAULT, "agent": "Alpha"}, **data)}
            ds.write_private(ds._pending_path(pid), json.dumps(rec))
            ds.cmd_status_verify([pid])
        self.assertEqual(self.sent(), [])

    def test_no_key_falls_back_to_an_ordinary_prompt(self):
        self.key = None
        self.prompt("status of all my agents")
        self.assertIsNone(self.prompt(f"undo {self.n_of(I_DEFAULT)}"))
        self.prompt("status of all my agents")
        self.assertIsNone(self.prompt(f"{self.n_of(I_HANDOFF)}: Gamma"))
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.spawned and [a for a in self.spawned if "status-verify" in a], [])

    def test_no_token_or_key_in_anything_shown(self):
        shown = [self.shown, self.prompt(f"undo {self.n_of(I_DEFAULT)}"), json.dumps(self.last_hook)] + self.outcomes()
        self.prompt("status of all my agents")
        shown += [self.prompt(f"{self.n_of(I_HANDOFF)}: Gamma"), json.dumps(self.last_hook)] + self.outcomes()
        for t in shown:
            self.assertNotIn("dsat_", t)
            self.assertNotIn(KEY, t)
        for n in os.listdir(ds.STATE):
            with open(os.path.join(ds.STATE, n), errors="replace") as f:
                self.assertNotIn(KEY, f.read(), n)


class UndoProofBytes(unittest.TestCase):
    """Vectors made with node 22 from x-live #493's formula (key dsak_k*43, grant aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee,
    workbook and instance below, prompt-0001-abcdef, sha256("do 3"), typed_at 1700000000000, iat 1700000003000)."""
    VECTORS = [
        (UNDO_LABEL, "undo_default", "cd25d8fee1365c253dccf23f266afeeb16fddc267ea8140040ee58e47273e150"),
        (ANSWER_LABEL, "undo_default", "99570322482248194d9ecd7fff7f480f905fc485fd77bfada7f1b7de017827ee"),
        (ANSWER_LABEL, "Gamma", "2dd0ff351fc8f57f60b2d54ba4fceaa62b149d4c4dd2e3cfda3076522bed7049"),
        (ANSWER_LABEL, "keep waiting", "0668dc72fd11b4030ab76bf56ada43c61f2d578d37ff0c8939c9d1d5536b7822"),
    ]

    def test_node_vectors(self):
        for label, answer, want in self.VECTORS:
            with self.subTest(label=label, answer=answer):
                p = ds.typed_proof(KEY, GRANT, WB, "99999999-8888-4777-8666-555555555555", answer, "prompt-0001-abcdef",
                                   "do 3", 1700000000000, 1700000003000, label=label)
                self.assertEqual(p["mac"], want)

    def test_the_undo_label_signs_only_the_fixed_answer_and_no_other_label_is_signed(self):
        args = (KEY, GRANT, WB, "99999999-8888-4777-8666-555555555555")
        with self.assertRaises(ValueError):
            ds.typed_proof(*args, "Gamma", "prompt-0001-abcdef", "do 3", 1, 2, label=UNDO_LABEL)
        with self.assertRaises(ValueError):
            ds.typed_proof(*args, "yes", "prompt-0001-abcdef", "do 3", 1, 2, label="datastash-typed-answer/v2")


if __name__ == "__main__":
    unittest.main(verbosity=1)
