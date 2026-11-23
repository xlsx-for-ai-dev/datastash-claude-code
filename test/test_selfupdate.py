#!/usr/bin/env python3
"""Offline tests for XLI-390 P2 (plugin 1.0.0): the plugin updates itself, says so once, names its version on every call,
and a release is tagged and pinned only at a reviewed commit.
- Self-update: at most once a day; only a vX.Y.Z tag newer than this copy counts (a branch head never does, also a
  newer-looking one); then Claude Code's own `claude plugin marketplace update datastash`, and `claude plugin update
  datastash@datastash` only when the refreshed catalog pins exactly that tag's commit. Same, older or untagged: nothing
  runs. A copy not installed from the datastash marketplace never updates itself.
- The next session start of a newer version says "datastash updated to vX" once.
- Every REST call sends X-Datastash-Plugin-Version and a User-Agent with the real version.
- tools/release-gate: refuses to tag (or pin, or approve main) without a recorded PASS at that exact commit; the pin is
  set only by its pin step (this branch's catalog carries none).
- The person's typed "turn off self-update" / "turn on self-update" is saved by the plugin (only the whole typed prompt
  counts); while it is off, self_update() checks nothing and runs nothing.
No network, no Claude Code run (both are faked); a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_selfupdate.py"""
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import ds, TMP, Base as StatusBase  # noqa: E402  (the same harness and plugin copy)
import harness  # noqa: E402  (tmpdir: a test's own temp folders go when it ends)
import time  # noqa: E402
import uuid  # noqa: E402

ROOT = os.path.join(HERE, "..")
GATE = os.path.join(ROOT, "tools", "release-gate")
A, B, C, D = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
GIT_ID = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
          "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *args):
    return subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-C", cwd] + list(args),
                          capture_output=True, text=True, check=True, env=dict(os.environ, **GIT_ID)).stdout.strip()


def tags(*rows):
    return "".join(f"{sha}\t{ref}\n" for sha, ref in rows)


def catalog(ref, sha, kind="git-subdir"):
    return {"name": "datastash", "plugins": [{"name": "datastash", "source": {
        "source": kind, "url": "https://github.com/xlsx-for-ai-dev/datastash-claude-code.git",
        "path": "plugins/datastash", "ref": ref, "sha": sha}}]}


class Base(unittest.TestCase):
    def patch(self, name, value):
        old = getattr(ds, name)
        setattr(ds, name, value)
        self.addCleanup(setattr, ds, name, old)

    def setUp(self):
        for p in (ds.SELF_UPDATE_FILE, ds.VERSION_SEEN_FILE):
            try:
                os.unlink(p)
            except OSError:
                pass
        if os.path.exists(ds.CONSENT_PATH):  # the person's choices start fresh (self-update on)
            os.replace(ds.CONSENT_PATH, os.path.join(TMP, f"old-consent-{time.time()}"))


class NewestRelease(Base):
    """The fake tag lists: only vX.Y.Z tags count, an annotated tag counts by its commit."""

    def test_newest_tag_wins_and_annotated_tags_peel(self):
        text = tags((A, "refs/tags/v0.9.9"), (B, "refs/tags/v0.9.9^{}"), (C, "refs/tags/v1.0.0"),
                    (D, "refs/tags/v0.10.0"))
        self.assertEqual(ds.newest_release(text), ("1.0.0", C))
        self.assertEqual(ds.newest_release(tags((A, "refs/tags/v1.2.0"), (B, "refs/tags/v1.2.0^{}"))), ("1.2.0", B))
        self.assertEqual(ds.newest_release(tags((A, "refs/tags/v1.2.0^{}"), (B, "refs/tags/v1.2.0"))), ("1.2.0", A))

    def test_heads_and_other_tags_never_count(self):
        text = tags((A, "refs/heads/main"), (B, "refs/heads/v9.0.0"), (C, "refs/tags/v9.0.0-rc1"),
                    (D, "refs/tags/latest"), (D, "refs/pull/1/head"))
        self.assertIsNone(ds.newest_release(text))
        self.assertIsNone(ds.newest_release(""))
        self.assertIsNone(ds.newest_release(None))

    def test_real_git_ls_remote_on_a_local_repo_ignores_the_untagged_head(self):
        repo = os.path.realpath(harness.tmpdir(self, prefix="rel-repo-", dir=TMP))
        git(repo, "init", "-q", "-b", "main")
        commits = []
        for i in range(3):
            with open(os.path.join(repo, "f"), "w") as f:
                f.write(str(i))
            git(repo, "add", "f")
            git(repo, "commit", "-q", "-m", str(i))
            commits.append(git(repo, "rev-parse", "HEAD"))
        git(repo, "tag", "-a", "v0.9.9", "-m", "x", commits[0])
        git(repo, "tag", "v1.0.0", commits[1])  # lightweight
        # commits[2] is main's head, untagged
        got = ds.newest_release(ds._ls_remote(repo))
        self.assertEqual(got, ("1.0.0", commits[1]))
        self.assertNotEqual(got[1], commits[2])

    def test_no_network_in_tests(self):
        self.assertIsNone(ds._ls_remote())  # https under DATASTASH_NO_NETWORK=1: not even tried


class SelfUpdate(Base):
    """The decisions, with the tag list, Claude Code and its catalog faked."""

    def setUp(self):
        super().setUp()
        self.calls, self.ls, self.cat, self.rc = [], tags((A, "refs/tags/v1.0.0")), None, 0
        self.patch("PLUGIN_VERSION", "1.0.0")
        self.patch("installed_from_marketplace", lambda root=None: True)
        self.patch("_ls_remote", lambda repo=ds.RELEASE_REPO: self.calls.append("ls-remote") or self.ls)
        self.patch("_run_claude", lambda args: self.calls.append(" ".join(args)) or self.rc)
        self.patch("marketplace_catalog", lambda: self.cat)

    def state(self):
        return ds.read_json(ds.SELF_UPDATE_FILE, {})

    def test_newer_tag_pinned_in_the_catalog_updates_through_claude_code(self):
        self.ls = tags((A, "refs/tags/v1.0.0"), (B, "refs/tags/v1.0.1"), (C, "refs/tags/v1.0.1^{}"),
                       (D, "refs/heads/main"))
        self.cat = catalog("v1.0.1", C)
        self.assertEqual(ds.self_update(now=1000), "updated")
        self.assertEqual(self.calls, ["ls-remote", "plugin marketplace update datastash",
                                      "plugin update datastash@datastash"])
        self.assertEqual((self.state()["result"], self.state()["updated_to"]), ("updated", "1.0.1"))

    def test_same_version_does_nothing(self):
        self.ls = tags((A, "refs/tags/v1.0.0"))
        self.assertEqual(ds.self_update(now=1000), "current")
        self.assertEqual(self.calls, ["ls-remote"])

    def test_older_version_does_nothing(self):
        self.ls = tags((A, "refs/tags/v0.9.9"), (B, "refs/tags/v0.9.10"))
        self.assertEqual(ds.self_update(now=1000), "current")
        self.assertEqual(self.calls, ["ls-remote"])

    def test_untagged_head_is_ignored(self):
        self.ls = tags((A, "refs/heads/main"), (B, "refs/heads/v2.0.0"), (C, "refs/tags/v0.9.9"))
        self.assertEqual(ds.self_update(now=1000), "current")
        self.ls = tags((A, "refs/heads/main"))
        self.assertEqual(ds.self_update(now=1000 + ds.SELF_UPDATE_EVERY), "no-release")
        self.assertEqual(self.calls, ["ls-remote", "ls-remote"])

    def test_catalog_not_pinned_to_that_tag_is_left_alone(self):
        self.ls = tags((B, "refs/tags/v1.1.0"))
        for i, cat in enumerate((catalog("main", D), catalog("v1.1.0", D), catalog("v1.0.9", B),
                                 {"name": "datastash", "plugins": [{"name": "datastash", "source": "./plugins/datastash"}]},
                                 catalog("v1.1.0", "B" * 40), None)):
            self.calls.clear()
            self.cat = cat
            self.assertEqual(ds.self_update(now=1000 + i * ds.SELF_UPDATE_EVERY), "pin-mismatch", cat)
            self.assertNotIn("plugin update datastash@datastash", self.calls)

    def test_at_most_once_a_day_even_when_it_failed(self):
        self.ls = None  # unreachable
        self.assertEqual(ds.self_update(now=1000), "no-release")
        self.assertEqual(ds.self_update(now=1000 + 3600), "recent")
        self.assertEqual(ds.self_update(now=1000 + ds.SELF_UPDATE_EVERY - 1), "recent")
        self.assertEqual(self.calls, ["ls-remote"])
        self.assertEqual(ds.self_update(now=1000 + ds.SELF_UPDATE_EVERY), "no-release")
        self.assertEqual(self.calls, ["ls-remote", "ls-remote"])

    def test_failed_claude_steps_stop_there(self):
        self.ls, self.cat, self.rc = tags((B, "refs/tags/v1.1.0")), catalog("v1.1.0", B), 1
        self.assertEqual(ds.self_update(now=1000), "marketplace-failed")
        self.assertEqual(self.calls, ["ls-remote", "plugin marketplace update datastash"])

    def test_a_development_copy_never_updates_itself(self):
        self.patch("installed_from_marketplace", lambda root=None: False)
        self.assertEqual(ds.self_update(now=1000), "not-installed")
        self.assertEqual(self.calls, [])

    def test_turned_off_by_the_person_nothing_is_checked_or_run(self):
        self.ls, self.cat = tags((B, "refs/tags/v1.1.0")), catalog("v1.1.0", B)
        ds.set_self_update(False)
        self.assertEqual(ds.self_update(now=1000), "off")
        self.assertEqual(self.calls, [])  # no tag check, no Claude Code run
        self.assertEqual(self.state(), {})  # not stamped: turning it back on checks at once
        ds.set_self_update(True)
        self.assertEqual(ds.self_update(now=1000 + 60), "updated")
        self.assertEqual(self.calls, ["ls-remote", "plugin marketplace update datastash",
                                      "plugin update datastash@datastash"])

    def test_hook_prints_nothing_and_exits_0(self):
        self.ls, self.cat = tags((B, "refs/tags/v1.1.0")), catalog("v1.1.0", B)
        self.patch("hook_input", lambda: {})
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ds.cmd_self_update([]), 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(self.state()["result"], "updated")
        self.patch("self_update", lambda now=None: 1 / 0)
        self.assertEqual(ds.cmd_self_update([]), 0)


class InstalledFrom(Base):
    def test_only_the_marketplace_cache_copy(self):
        home = os.path.join(TMP, "h")
        self.assertTrue(ds.installed_from_marketplace(os.path.join(home, ".claude/plugins/cache/datastash/datastash/1.0.0")))
        for p in (os.path.join(home, "src/datastash-claude-code/plugins/datastash"),
                  os.path.join(home, ".claude/plugins/cache/other/datastash/1.0.0"),
                  os.path.join(home, ".claude/plugins/marketplaces/datastash/plugins/datastash")):
            self.assertFalse(ds.installed_from_marketplace(p), p)


    def test_the_catalog_is_read_from_the_plugins_folder_it_was_installed_in(self):
        plugins = os.path.realpath(harness.tmpdir(self, prefix="plugins-", dir=TMP))
        market = os.path.join(plugins, "elsewhere", "datastash-market")
        os.makedirs(os.path.join(market, ".claude-plugin"))
        with open(os.path.join(market, ".claude-plugin", "marketplace.json"), "w") as f:
            json.dump(catalog("v1.0.0", A), f)
        with open(os.path.join(plugins, "known_marketplaces.json"), "w") as f:
            json.dump({"datastash": {"installLocation": market}}, f)
        root = os.path.join(plugins, "cache", "datastash", "datastash", "1.0.0")
        self.assertEqual(ds.catalog_pin(ds.marketplace_catalog(root)), ("v1.0.0", A))
        with open(os.path.join(plugins, "known_marketplaces.json"), "w") as f:
            json.dump({}, f)  # no record: Claude Code's default folder
        self.assertIsNone(ds.marketplace_catalog(root))


class UpdatedLine(Base):
    def test_said_once_at_the_first_start_of_a_newer_version(self):
        self.patch("PLUGIN_VERSION", "0.9.10")
        self.assertIsNone(ds.updated_line())  # first start ever: recorded, nothing said
        self.assertIsNone(ds.updated_line())
        ds.PLUGIN_VERSION = "1.0.0"
        self.assertEqual(ds.updated_line(), "datastash updated to v1.0.0")
        self.assertIsNone(ds.updated_line())  # once
        ds.PLUGIN_VERSION = "0.9.10"
        self.assertIsNone(ds.updated_line())  # a step back is not an update

    def test_session_start_shows_it_to_the_person(self):
        self.patch("PLUGIN_VERSION", "1.0.0")
        ds.updated_line()
        ds.PLUGIN_VERSION = "1.0.1"
        folder = harness.tmpdir(self, prefix="upd-", dir=TMP)
        self.patch("hook_input", lambda: {"session_id": "sess-upd", "cwd": folder})
        self.patch("off_here", lambda inp: False)
        self.patch("session_sign_in", lambda inp: (None, None))
        self.patch("resolve", lambda p, me=None: (None, []))
        outs = []
        for _ in range(2):
            out = io.StringIO()
            with redirect_stdout(out):
                ds.cmd_check([])
            outs.append(json.loads(out.getvalue()))
        self.assertEqual(outs[0]["systemMessage"], "datastash updated to v1.0.1")
        self.assertNotIn("datastash updated", outs[1].get("systemMessage", ""))


class VersionOnEveryCall(Base):
    def test_header_and_user_agent_carry_the_real_version(self):
        with open(os.path.join(ROOT, "plugins", "datastash", ".claude-plugin", "plugin.json")) as f:
            real = json.load(f)["version"]
        self.assertEqual(ds.PLUGIN_VERSION, real)
        seen = []

        class Resp:
            status = 200
            headers = {"Content-Type": "application/json"}

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        self.patch("_opener", type("O", (), {"open": lambda self, req, timeout=None: seen.append(req) or Resp()})())
        old = os.environ.get("DATASTASH_NO_NETWORK")
        os.environ["DATASTASH_NO_NETWORK"] = "0"  # the opener is faked: nothing leaves this process
        self.addCleanup(os.environ.__setitem__, "DATASTASH_NO_NETWORK", old)
        ds.http("GET", "/api/v1/agent/inbox", key="k")
        h = {k.lower(): v for k, v in seen[0].header_items()}
        self.assertEqual(h["x-datastash-plugin-version"], real)
        self.assertEqual(h["user-agent"], "datastash-claude-code-plugin/" + real)


class Wiring(Base):
    def test_session_start_runs_it_in_the_background_without_waking_the_session(self):
        with open(os.path.join(ROOT, "plugins", "datastash", "hooks", "hooks.json")) as f:
            hooks = json.load(f)["hooks"]
        ups = [h for ev in hooks for g in hooks[ev] for h in g["hooks"] if h["args"][-1] == "self-update"]
        self.assertEqual(len(ups), 1)
        self.assertIn(ups[0], [h for g in hooks["SessionStart"] for h in g["hooks"]])
        self.assertTrue(ups[0]["async"])
        self.assertNotIn("asyncRewake", ups[0])

    def test_the_branch_catalog_carries_no_pin(self):
        # Review 1: the pin is set only by the release step (release-gate pin, checked by release-gate main).
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as f:
            cat = json.load(f)
        self.assertIsNone(ds.catalog_pin(cat))
        self.assertEqual(next(p for p in cat["plugins"] if p["name"] == "datastash")["source"], "./plugins/datastash")

    def test_the_menu_readme_and_hooks_name_the_off_switch(self):
        self.assertTrue(any('"turn off self-update"' in x and '"turn on self-update"' in x for x in ds.INSTALL_TAIL))
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        with open(os.path.join(ROOT, "plugins", "datastash", "hooks", "hooks.json")) as f:
            desc = json.load(f)["description"]
        for text in (readme, desc):
            self.assertIn('"turn off self-update"', text)
            self.assertIn('"turn on self-update"', text)


class OffSwitch(StatusBase):
    """The person's typed phrase, caught and saved like the clean sessions phrases: only the whole typed prompt."""

    def prompt(self, text, sid="sess1"):
        return self.hook(ds.cmd_prompt, {"session_id": sid, "hook_event_name": "UserPromptSubmit", "prompt": text,
                                         "cwd": harness.tmpdir(self, prefix="su-", dir=TMP), "prompt_id": str(uuid.uuid4()),
                                         "transcript_path": self.transcript(sid)})

    @staticmethod
    def ctx(out):
        return ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""

    def test_on_from_the_start(self):
        self.assertTrue(ds.self_update_on())

    def test_the_phrases_are_recognised_forgivingly(self):
        for t in ("turn off self-update", "Turn off self update.", '  "turn off selfupdate!"', "self-update off",
                  "turn off datastash self-updates"):
            self.assertEqual(ds.self_update_phrase(t), "off", t)
        for t in ("turn on self-update", "Turn on self update", "self-update on"):
            self.assertEqual(ds.self_update_phrase(t), "on", t)
        for t in ("please turn off self-update for the other computer later", "self-update", "turn off updates", "",
                  None, "turn off self-update " + "x" * 300):
            self.assertIsNone(ds.self_update_phrase(t), t)

    def test_turn_off_then_on_is_saved_and_confirmed_in_one_line(self):
        out = self.prompt("turn off self-update")
        self.assertFalse(ds.self_update_on())
        with open(ds.CONSENT_PATH) as f:
            self.assertIs(json.load(f)["self_update"]["on"], False)
        ctx = self.ctx(out)
        self.assertIn(ds.SELF_UPDATE_NOTE, ctx)
        self.assertIn("one line", ctx)
        self.assertIn(ds.SELF_UPDATE_OFF_SAY, ctx)
        out = self.prompt("turn on self-update")
        self.assertTrue(ds.self_update_on())
        self.assertIn(ds.SELF_UPDATE_ON_SAY, self.ctx(out))

    def test_kept_beside_the_other_per_person_choices(self):
        ds.set_clean(no_code=True)
        self.prompt("turn off self-update")
        self.assertEqual(ds.clean_settings(), {"commands": True, "no_code": True})
        ds.set_clean(commands=False)
        self.assertFalse(ds.self_update_on())

    def test_an_ordinary_prompt_or_a_forged_hook_changes_nothing(self):
        self.prompt("can you turn off self-update on my other computer later")
        self.assertTrue(ds.self_update_on())
        self.assertIsNone(ds.self_update_prompt_context({"prompt": "turn off self-update"}))  # not the hook's shape
        self.assertIsNone(ds.self_update_prompt_context({"hook_event_name": "UserPromptSubmit",
                                                         "prompt": "turn off self-update", "session_id": "s"}))
        self.assertTrue(ds.self_update_on())

    def test_a_broken_value_means_on(self):
        ds.write_private(ds.CONSENT_PATH, json.dumps({"self_update": {"on": "no"}}))
        self.assertTrue(ds.self_update_on())
        ds.write_private(ds.CONSENT_PATH, json.dumps({"self_update": False}))
        self.assertTrue(ds.self_update_on())


class ReleaseGate(Base):
    """tools/release-gate in a throwaway repo."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.realpath(harness.tmpdir(self, prefix="gate-repo-", dir=TMP))
        self.records = os.path.realpath(harness.tmpdir(self, prefix="gate-rec-", dir=TMP))
        git(self.repo, "init", "-q", "-b", "main")
        os.makedirs(os.path.join(self.repo, "plugins/datastash/.claude-plugin"))
        os.makedirs(os.path.join(self.repo, ".claude-plugin"))
        self.write_cat(catalog("v0.9.9", A))
        self.release("0.9.9")
        git(self.repo, "tag", "-a", "v0.9.9", "-m", "x")

    def write_cat(self, cat):
        with open(os.path.join(self.repo, ".claude-plugin/marketplace.json"), "w") as f:
            json.dump(cat, f)

    def release(self, v):
        with open(os.path.join(self.repo, "plugins/datastash/.claude-plugin/plugin.json"), "w") as f:
            json.dump({"name": "datastash", "version": v}, f)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", v)
        return git(self.repo, "rev-parse", "HEAD")

    def record(self, line):
        with open(os.path.join(self.records, "review.txt"), "a") as f:
            f.write(line + "\n")

    def gate(self, *args):
        r = subprocess.run([sys.executable, "-I", GATE] + list(args) + ["--records", self.records], cwd=self.repo,
                           capture_output=True, text=True, env=dict(os.environ, **GIT_ID), timeout=60)
        return r.returncode, r.stdout + r.stderr

    def tag_list(self):
        return git(self.repo, "tag", "--list").split()

    def test_tag_refused_without_a_pass_at_the_head(self):
        self.release("1.0.0")
        rc, out = self.gate("tag")
        self.assertEqual(rc, 1, out)
        self.assertIn("no recorded independent PASS", out)
        self.assertEqual(self.tag_list(), ["v0.9.9"])

    def test_a_pass_for_another_commit_does_not_count(self):
        old = git(self.repo, "rev-parse", "HEAD")
        self.record(f"opus review: PASS at {old}")
        self.release("1.0.0")
        self.assertEqual(self.gate("tag")[0], 1)
        self.assertEqual(self.tag_list(), ["v0.9.9"])

    def test_tag_with_a_pass_and_never_twice(self):
        head = self.release("1.0.0")
        self.record(f"opus review (cycle 2): PASS at {head[:7]}")
        rc, out = self.gate("tag")
        self.assertEqual(rc, 0, out)
        self.assertEqual(git(self.repo, "rev-list", "-n", "1", "v1.0.0"), head)
        self.assertEqual(self.gate("tag")[0], 1)  # v1.0.0 exists

    def test_a_fail_at_the_head_refuses(self):
        head = self.release("1.0.0")
        self.record(f"opus review: PASS at {head}")
        self.record(f"opus review: FAIL at {head}")
        rc, out = self.gate("tag")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL", out)

    def test_version_must_go_up_and_the_tree_be_clean(self):
        head = self.release("0.9.8")
        self.record(f"opus review: PASS at {head}")
        self.assertEqual(self.gate("tag")[0], 1)
        head = self.release("1.0.0")
        self.record(f"opus review: PASS at {head}")
        with open(os.path.join(self.repo, "stray"), "w") as f:
            f.write("x")
        rc, out = self.gate("tag")
        self.assertEqual(rc, 1)
        self.assertIn("not clean", out)

    def test_no_records_folder_refuses(self):
        r = subprocess.run([sys.executable, "-I", GATE, "tag"], cwd=self.repo, capture_output=True, text=True,
                           env=dict(os.environ, **GIT_ID), timeout=60)
        self.assertEqual(r.returncode, 1)

    def test_pin_then_main(self):
        r = self.release("1.0.0")
        self.record(f"opus review: PASS at {r}")
        self.assertEqual(self.gate("tag")[0], 0)
        rc, out = self.gate("pin", "v1.0.0")
        self.assertEqual(rc, 0, out)
        with open(os.path.join(self.repo, ".claude-plugin/marketplace.json")) as f:
            self.assertEqual(ds.catalog_pin(json.load(f)), ("v1.0.0", r))
        git(self.repo, "commit", "-q", "-am", "pin v1.0.0")
        p = git(self.repo, "rev-parse", "HEAD")
        rc, out = self.gate("main")
        self.assertEqual(rc, 1)  # the pin commit itself is not reviewed yet
        self.record(f"opus review: PASS at {p}")
        rc, out = self.gate("main")
        self.assertEqual(rc, 0, out)

    def test_pin_turns_the_unpinned_catalog_into_the_tag_pin(self):
        self.write_cat({"name": "datastash", "plugins": [{"name": "datastash", "source": "./plugins/datastash",
                                                          "description": "d"}]})
        r = self.release("1.0.0")
        self.record(f"opus review: PASS at {r}")
        self.assertEqual(self.gate("main")[0], 1)  # no pin: main refused
        self.assertEqual(self.gate("tag")[0], 0)
        rc, out = self.gate("pin", "v1.0.0")
        self.assertEqual(rc, 0, out)
        with open(os.path.join(self.repo, ".claude-plugin/marketplace.json")) as f:
            self.assertEqual(ds.catalog_pin(json.load(f)), ("v1.0.0", r))

    def test_pin_refused_for_an_unreviewed_tag_and_main_for_a_wrong_pin(self):
        r = self.release("1.0.0")
        git(self.repo, "tag", "-a", "v1.0.0", "-m", "made by hand")
        self.assertEqual(self.gate("pin", "v1.0.0")[0], 1)
        self.write_cat(catalog("v1.0.0", "e" * 40))
        git(self.repo, "commit", "-q", "-am", "wrong pin")
        self.record(f"opus review: PASS at {git(self.repo, 'rev-parse', 'HEAD')}")
        self.record(f"opus review: PASS at {r}")
        rc, out = self.gate("main")
        self.assertEqual(rc, 1)
        self.assertIn("not that tag's commit", out)


if __name__ == "__main__":
    unittest.main()
