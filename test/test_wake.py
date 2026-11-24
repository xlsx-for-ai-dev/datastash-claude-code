#!/usr/bin/env python3
"""Offline tests for XLI-374 (11-24, plugin 1.0.4): every datastash message wakes the receiving agent by itself.
- Standby: a session whose agent another session still listens for waits beside it (local only, one standby per
  session) and takes over once that listener lets go, so a restarted session is never deaf until its next turn. While
  the last holder's Claude Code process still runs it gets a grace period to listen again first.
- A fault the listener does not know (any exception) is retried after the backoff, inside the loop and at its start.
- The idle notice (Notification idle_prompt) starts the listener again; with this session's listener running it ends.
- XLI-371 still holds: an item that arrives handled (datastash's own receipts, an fyi) never wakes.
No network, no key; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_wake.py"""
import io
import json
import os
import sys
import time
import unittest
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_plugin import Base, Stop, ds, AGENT  # noqa: E402  (the same harness, plugin copy and fixtures)

S = AGENT["slug"]


def _offline(method, path, key=None, body=None, form=None, timeout=40, accept="application/json"):
    raise ds.urllib.error.URLError("offline test")


class WakeBase(Base):
    def setUp(self):
        super().setUp()
        for k in ("send_beat", "_listen_loop", "_still_mine", "STANDBY_GRACE_SECONDS", "_take_listener_lock"):
            self.saved[k] = getattr(ds, k)
        ds.http = _offline
        ds.send_beat = lambda agent, key, project: None
        self.held = []
        self.addCleanup(self._let_go)
        ds._LISTEN_PARENT[:] = []
        self.addCleanup(lambda: ds._LISTEN_PARENT.__setitem__(slice(None), []))

    def _let_go(self):
        for fd in self.held:
            try:
                os.close(fd)
            except OSError:
                pass
        self.held = []

    def hold(self, session_id, parent=None):
        """Another listener (another session) holds the agent's lock: its own open file, as another process would."""
        ds._LISTEN_PARENT[:] = list(parent) if parent else []
        fd = ds._take_listener_lock(S, session_id)
        ds._LISTEN_PARENT[:] = []
        self.assertIsNotNone(fd)
        self.held.append(fd)
        return fd

    def release(self, fd):
        os.close(fd)
        self.held.remove(fd)

    def listen(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = ds.cmd_listen([])
        return rc, err.getvalue()

    def log_text(self):
        try:
            with open(ds.log_path(S)) as f:
                return f.read()
        except OSError:
            return ""


class Standby(WakeBase):
    def test_a_session_whose_agent_another_session_holds_waits_and_takes_over(self):
        """A restarted session (the old one has not let go yet) no longer gives up: it takes the lock once it is free."""
        other = self.hold("old-session")
        sleeps = []

        def sl(sec, parent):
            sleeps.append(sec)
            if len(sleeps) == 2:
                self.release(other)  # the old session ended: its listener stopped
            if len(sleeps) > 20:
                raise Stop()
        ds._sleep = sl
        polled = []

        def fetch(agent, key, cursor, wait, timeout=None):
            polled.append(ds.read_json(ds.lock_path(S), {}).get("session_id"))
            raise Stop()
        ds.fetch_page = fetch
        with self.assertRaises(Stop):
            self.listen()
        self.assertEqual(polled, ["s1"])  # this session holds the lock and polls
        self.assertEqual(sleeps[:2], [ds.PARK_CHECK_SECONDS] * 2)
        self.assertIn("this session listens now", self.log_text())

    def test_the_standby_makes_no_request_while_it_waits(self):
        other = self.hold("old-session")
        calls = []
        ds.http = lambda *a, **k: calls.append(a) or _offline(*a, **k)
        ds.read_key = lambda s: calls.append(("key", s)) or "ldgr_ag_test"
        n = []

        def sl(sec, parent):
            n.append(1)
            if len(n) >= 4:
                raise Stop()
        ds._sleep = sl
        with self.assertRaises(Stop):
            self.listen()
        self.assertEqual(calls, [])
        self.release(other)

    def test_this_session_s_own_listener_running_ends_the_hook_at_once(self):
        """The idle notice (or a second hook) of a session that already listens: exit 0, no wait."""
        self.hold("s1")
        ds._sleep = lambda sec, parent: (_ for _ in ()).throw(AssertionError("waited"))
        self.assertEqual(self.listen(), (0, ""))

    def test_one_standby_per_session(self):
        self.hold("old-session")
        sb = ds._take_standby_lock(S, "s1")
        self.assertIsNotNone(sb)
        self.held.append(sb)
        ds._sleep = lambda sec, parent: (_ for _ in ()).throw(AssertionError("waited"))
        self.assertEqual(self.listen(), (0, ""))
        # another session gets its own standby
        sb2 = ds._take_standby_lock(S, "s2")
        self.assertIsNotNone(sb2)
        self.held.append(sb2)

    def test_the_standby_stops_when_its_session_ends_or_its_folder_lets_go(self):
        self.hold("old-session")
        alive = {"v": True}
        real = ds._parent_alive
        ds._parent_alive = lambda pid: alive["v"]
        self.addCleanup(setattr, ds, "_parent_alive", real)

        def sl(sec, parent):
            alive["v"] = False
        ds._sleep = sl
        self.assertEqual(self.listen(), (0, ""))
        self.assertIn("standby's session gone", self.log_text())
        # the folder now belongs to another agent
        alive["v"] = True
        ds._sleep = lambda sec, parent: None
        ds._still_mine = lambda s, project, me: False
        self.assertEqual(self.listen(), (0, ""))
        self.assertIn("no longer listens for this agent", self.log_text())

    def test_a_live_holder_session_gets_its_grace_then_the_standby_takes_over(self):
        """The last holder's Claude Code process still runs (it was woken, its turn is going on): the standby leaves it
        the grace period to listen again first, then takes over (a session that never listens again is healed)."""
        live_pid = os.getppid()  # a live process that is not this session's
        other = self.hold("old-session", parent=(live_pid, ds._proc_start(live_pid)))
        ds.STANDBY_GRACE_SECONDS = 0.3
        t0 = []

        def sl(sec, parent):
            if not t0:
                self.release(other)  # it was woken (exit 2) and has not started listening again
                t0.append(time.time())
            time.sleep(0.02)
        ds._sleep = sl
        took = []

        def fetch(agent, key, cursor, wait, timeout=None):
            took.append(time.time() - t0[0])
            raise Stop()
        ds.fetch_page = fetch
        with self.assertRaises(Stop):
            self.listen()
        self.assertEqual(len(took), 1)
        self.assertGreaterEqual(took[0], 0.3)

    def test_a_holder_that_listens_again_within_the_grace_keeps_its_agent(self):
        live_pid = os.getppid()
        rec = (live_pid, ds._proc_start(live_pid))
        other = self.hold("old-session", parent=rec)
        self.release(other)
        ds.STANDBY_GRACE_SECONDS = 3600
        n = []

        def sl(sec, parent):
            n.append(1)
            if len(n) == 2:
                self.hold("old-session", parent=rec)  # its Stop hook started its listener again
            if len(n) >= 5:
                raise Stop()
        ds._sleep = sl
        ds.fetch_page = lambda *a, **k: (_ for _ in ()).throw(AssertionError("the standby took the lock"))
        with self.assertRaises(Stop):
            self.listen()

    def test_session_end_stops_this_session_s_standby(self):
        self.hold("old-session")
        sb = ds._take_standby_lock(S, "s-end")
        self.held.append(sb)
        killed = []
        real_kill, real_is = ds.os.kill, ds._is_listener
        ds.os.kill = lambda pid, sig: killed.append(pid)
        ds._is_listener = lambda pid: True
        try:
            ds.stop_session_listeners("s-end")
        finally:
            ds.os.kill, ds._is_listener = real_kill, real_is
        self.assertEqual(killed, [os.getpid()])

    def test_the_lock_names_the_holder_s_session_process(self):
        ds._LISTEN_PARENT[:] = [4242, "start-A"]
        fd = ds._take_listener_lock(S, "s1")
        self.held.append(fd)
        rec = ds.read_json(ds.lock_path(S), {})
        self.assertEqual((rec["parent"], rec["parent_start"], rec["session_id"]), (4242, "start-A", "s1"))
        self.assertFalse(ds._holder_session_live(rec, 4242))  # this session's own process is never "the other one"


class Heals(WakeBase):
    def test_an_unexpected_error_in_the_loop_is_retried_not_the_end(self):
        n = []

        def fetch(agent, key, cursor, wait, timeout=None):
            n.append(1)
            if len(n) == 1:
                raise RuntimeError("a fault the loop does not know")
            raise Stop()
        ds.fetch_page = fetch
        ds._sleep = lambda sec, parent: None
        with self.assertRaises(Stop):
            self.listen()
        self.assertEqual(len(n), 2)
        self.assertIn("unexpected RuntimeError", self.log_text())
        self.assertNotIn("a fault the loop does not know", self.log_text())  # by type only

    def test_an_unexpected_error_at_start_starts_over(self):
        n = []

        def loop(*a, **k):
            n.append(1)
            if len(n) == 1:
                raise KeyError("x")
            return 0
        ds._listen_loop = loop
        waits = []
        ds._sleep = lambda sec, parent: waits.append(sec)
        self.assertEqual(self.listen(), (0, ""))
        self.assertEqual(len(n), 2)
        self.assertEqual(len(waits), 1)
        self.assertGreaterEqual(waits[0], ds.CRASH_BACKOFF_START * 0.8)

    def test_a_start_that_keeps_failing_ends_with_its_session(self):
        ds._listen_loop = lambda *a, **k: (_ for _ in ()).throw(ValueError("x"))
        alive = iter([True, True, False])
        real = ds._parent_alive
        ds._parent_alive = lambda pid: next(alive)
        self.addCleanup(setattr, ds, "_parent_alive", real)
        waits = []
        ds._sleep = lambda sec, parent: waits.append(sec)
        self.assertEqual(self.listen(), (0, ""))
        self.assertEqual(len(waits), 3)
        self.assertLess(waits[0], waits[1])  # a growing wait

    def test_the_watch_command_is_not_retried(self):
        ds._listen_loop = lambda *a, **k: (_ for _ in ()).throw(ValueError("x"))
        with self.assertRaises(ValueError):
            ds.cmd_listen([], printer=lambda t: None)


class Hooks(unittest.TestCase):
    def test_the_idle_notice_starts_the_listener_too(self):
        with open(os.path.join(HERE, "..", "plugins", "datastash", "hooks", "hooks.json")) as f:
            hooks = json.load(f)["hooks"]
        idle = [h for g in hooks.get("Notification", []) if g.get("matcher") == "idle_prompt" for h in g["hooks"]]
        self.assertEqual(len(idle), 1)
        h = idle[0]
        self.assertEqual(h["args"][-1], "listen")
        self.assertTrue(h["async"] and h["asyncRewake"])
        self.assertEqual(h["timeout"], 2000000)
        # The idle notice is the only Notification hook: no other notice starts a listener.
        self.assertEqual([g.get("matcher") for g in hooks["Notification"]], ["idle_prompt"])


class Receipts(Base):
    def test_an_item_that_arrives_handled_never_wakes(self):
        """XLI-371: datastash's own receipts and fyi messages arrive handled: never counted new, never a wake."""
        a = dict(AGENT, slug="rcpt")
        st = ds.load_state(a)
        items = [{"message_id": 7, "status": "handled", "kind": "fyi", "from": "datastash"}]
        applied, n, fresh = ds.apply_page(a, st.get("cursor"), items, "7", False)
        self.assertTrue(applied)
        self.assertEqual((n, fresh), (0, 0))
        items = [{"message_id": 8, "status": "new", "kind": "chatter", "from": "agent:friend"}]
        applied, n, fresh = ds.apply_page(a, "7", items, "8", False)
        self.assertEqual((n, fresh), (1, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
