#!/usr/bin/env python3
"""Offline tests for XLI-399 (plugin 1.0.4): the model can't reach datastash key material through any tool.
- The PreToolUse hook refuses every tool call (Read, Write, Edit, MultiEdit, NotebookEdit, Grep, Glob, Bash, Monitor,
  another MCP server's tools, an unknown tool) that names a file the plugin keeps in the datastash folder, the folder
  itself, or a main folder's marker, in every path form: absolute, relative to the session's folder or to a `cd`, ~,
  $HOME, ${HOME} and $XDG_CONFIG_HOME, a glob or a brace, a symlink to the folder or to one file, any letter case, a
  path split into pieces, and a keychain read of the datastash items. 1.0.9: the same for the engine login's folder
  (~/.xlsx-for-ai, all of it: its config.json holds the engine key xlsx-test reads).
- In the file tools (Read, Grep, Glob and the rest), a search of a folder that holds the datastash folder (home,
  ~/.config, /) is refused too, including a Grep with no path in such a folder. Bash and Monitor are refused only
  when their text names the folder or a key file in a literal form: normal repo work after a `cd` from a session
  started in the home folder (git add/commit, grep -rn, find -exec, xargs, tar) is never refused.
- The same refusal in the main session and in a subagent, with clean sessions on or off.
- Only the listener logs stay readable; ordinary project work, the datastash command and the plugin's own steps are
  never refused by it; the plugin's own key reads still work.
No network, no key: fake key files in a throwaway HOME, enforced by test/harness.py. No key file is ever printed.
Run: python3 -I test/test_keys.py"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness  # noqa: E402
from test_status import Base, ds  # noqa: E402  (the same harness and plugin copy)

HOME = harness.FAKE_HOME
DEFAULT = os.path.join(HOME, ".config", "datastash")  # where the plugin keeps its folder on a real computer
FAKE_AGENT_KEY = "ldgr_ag_" + "k" * 30  # made up, never a real key
SECRET_FILES = ("agent-web-app.key", "agent-web-app.proof", "agent-web-app.answer", "person.key", "main.key",
                "login.refresh", "login.answer", "login.access.json", "login.pending.json", "enroll.json",
                "hook-abc.secret", "main.json", "pending/agent-web-app.verifier", "state/agent-web-app.chatkey",
                "off/agent-old.key.20261124-000000")


def put(path, text):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


class KeyBase(Base):
    def setUp(self):
        super().setUp()
        for rel in SECRET_FILES:
            put(os.path.join(DEFAULT, rel), "made-up\n")
        put(os.path.join(DEFAULT, "state", "agent-web-app.log"), "listener started\n")
        put(os.path.join(ds.CONF, "agent-keys-test.key"), FAKE_AGENT_KEY + "\n")
        self.links = harness.tmpdir(self, prefix="links-", dir=harness.ROOT)
        os.symlink(DEFAULT, os.path.join(self.links, "conf-link"))
        os.symlink(os.path.join(DEFAULT, "login.answer"), os.path.join(self.links, "plain-name"))
        self.project = os.path.realpath(harness.tmpdir(self, prefix="kproj-", dir=os.path.join(HOME)))
        put(os.path.join(self.project, "src", "app.py"), "print('hi')\n")
        ds.set_clean(commands=False, no_code=False)

    def call(self, name, ti, cwd=None, agent_id=None):
        inp = {"session_id": "sess-keys", "hook_event_name": "PreToolUse", "tool_name": name, "tool_input": ti,
               "cwd": cwd or HOME}
        if agent_id:
            inp["agent_id"], inp["agent_type"] = agent_id, "general-purpose"
        return inp

    def key_denied(self, out):
        hs = (out or {}).get("hookSpecificOutput", {})
        return hs.get("permissionDecision") == "deny"

    def assert_refused(self, name, ti, cwd=None):
        out = ds.key_path_deny(self.call(name, ti, cwd))
        self.assertTrue(self.key_denied(out), f"{name} {ti} cwd={cwd}: not refused")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"], ds.KEY_DENY)

    def assert_allowed(self, name, ti, cwd=None):
        self.assertIsNone(ds.key_path_deny(self.call(name, ti, cwd)), f"{name} {ti} cwd={cwd}: refused")


# Every way a path to one key file can be written (cwd is HOME unless given)
KEY = "agent-web-app.key"
PATH_FORMS = (
    ("absolute", os.path.join(DEFAULT, KEY), None),
    ("relative to the session's folder", os.path.join(".config", "datastash", KEY), None),
    ("relative with ..", os.path.join("..", "..", ".config", "datastash", KEY), "PROJECT/src"),
    ("tilde", "~/.config/datastash/" + KEY, None),
    ("$HOME", "$HOME/.config/datastash/" + KEY, None),
    ("${HOME}", "${HOME}/.config/datastash/" + KEY, None),
    ("$XDG_CONFIG_HOME", "$XDG_CONFIG_HOME/datastash/" + KEY, None),
    ("glob on the folder", "~/.config/data*/" + KEY, None),
    ("glob on the file", "~/.config/datastash/agent-*", None),
    ("glob on both", "~/.c*/d?tastash/*", None),
    ("brace", "~/.config/{datastash,other}/" + KEY, None),
    ("symlink to the folder", "LINKS/conf-link/" + KEY, None),
    ("upper case", "~/.CONFIG/DataStash/AGENT-WEB-APP.KEY", None),
)


class EveryToolEveryForm(KeyBase):
    def form(self, raw, cwd):
        p = raw.replace("LINKS", self.links)
        c = (cwd or HOME).replace("PROJECT", self.project)
        return p, c

    def test_the_file_tools_refuse_every_path_form(self):
        for label, raw, cwd in PATH_FORMS:
            p, c = self.form(raw, cwd)
            for name, ti in (("Read", {"file_path": p}), ("Write", {"file_path": p, "content": "x"}),
                             ("Edit", {"file_path": p, "old_string": "a", "new_string": "b"}),
                             ("MultiEdit", {"file_path": p, "edits": []}),
                             ("NotebookEdit", {"notebook_path": p, "new_source": "x"}),
                             ("Grep", {"pattern": "x", "path": p}), ("Glob", {"pattern": "*", "path": p})):
                with self.subTest(form=label, tool=name):
                    self.assert_refused(name, ti, c)

    def test_commands_refuse_every_path_form(self):
        for label, raw, cwd in PATH_FORMS:
            p, c = self.form(raw, cwd)
            for name, ti in (("Bash", {"command": f"cat {p}"}), ("Bash", {"command": f'head -c 9 "{p}"'}),
                             ("Bash", {"command": f"cp {p} /tmp/x"}), ("Monitor", {"command": f"tail -f {p}"}),
                             ("mcp__files__read_file", {"path": p}), ("SomeNewTool", {"target": {"file": p}})):
                with self.subTest(form=label, tool=name):
                    self.assert_refused(name, ti, c)

    def test_a_symlink_to_one_key_file_is_refused(self):
        link = os.path.join(self.links, "plain-name")
        for name, ti in (("Read", {"file_path": link}), ("Bash", {"command": f"cat {link}"}),
                         ("Grep", {"pattern": ".", "path": link}), ("Monitor", {"command": f"tail {link}"})):
            with self.subTest(tool=name):
                self.assert_refused(name, ti)

    def test_every_file_the_plugin_keeps_is_refused(self):
        for rel in SECRET_FILES:
            p = os.path.join(DEFAULT, rel)
            for name, ti in (("Read", {"file_path": p}), ("Bash", {"command": f"cat {p}"})):
                with self.subTest(file=rel, tool=name):
                    self.assert_refused(name, ti)
        self.assert_refused("Read", {"file_path": os.path.join(ds.CONF, "agent-keys-test.key")})

    def test_a_cd_then_a_relative_path_is_refused(self):
        for cmd in ("cd ~/.config && cat datastash/agent-web-app.key", "cd ~/.config/datastash; cat person.key",
                    "pushd $HOME/.config/datastash >/dev/null && cat main.json", "cd .config/datastash && cat *"):
            with self.subTest(cmd=cmd):
                self.assert_refused("Bash", {"command": cmd})

    def test_a_split_or_escaped_path_is_refused(self):
        for cmd in ("python3 -c \"import os;print(open(os.path.expanduser('~/.con'+'fig/data'+'stash/person.key')).read())\"",
                    "cat ~/.config/data''stash/main.json", 'cat ~/.config/data"st"ash/main.json',
                    "cat ~/.config/d\\atastash/main.json", "node -e \"fs.readFileSync(HOME+'/.config/'+'datastash/x')\""):
            with self.subTest(cmd=cmd):
                self.assert_refused("Bash", {"command": cmd})

    def test_the_main_folders_marker_is_refused(self):
        marker = os.path.join(ds.MAIN_DIR, ds.MAIN_MARKER)
        for name, ti in (("Read", {"file_path": marker}), ("Bash", {"command": f"cat '{marker}'"}),
                         ("Grep", {"pattern": ".", "path": ds.MAIN_DIR})):
            with self.subTest(tool=name):
                self.assert_refused(name, ti)

    def test_a_keychain_read_of_the_datastash_items_is_refused(self):
        for cmd in ("secret-tool lookup service datastash account refresh-token",
                    "secret-tool lookup service datastash account agent-answer-web-app",
                    "secret-tool search --all service datastash", "secret-tool search --all --unlock x y",
                    "security find-generic-password -s datastash -w", "security dump-keychain -d",
                    "python3 -c \"import keyring;print(keyring.get_password('datastash','answer-key'))\""):
            with self.subTest(cmd=cmd):
                self.assert_refused("Bash", {"command": cmd})


class TwoStepRoutes(KeyBase):
    def test_chained_cds_are_followed(self):
        self.assert_refused("Bash", {"command": "cd ~; cd .config; cat datastash/person.key"}, cwd=self.project)
        self.assert_refused("Bash", {"command": "cd /; cd home; cd ..; cd " + HOME.lstrip("/") + " && cd .config && "
                                                "grep -r x datastash"}, cwd=self.project)

    def test_loading_the_plugin_script_to_call_its_key_readers_is_refused(self):
        for cmd in ("python3 -c \"import runpy;m=runpy.run_path('/p/scripts/datastash');print(m['read_key']('web-app'))\"",
                    "python3 - <<'E'\nexec(open('/p/scripts/datastash').read()); print(login_token())\nE",
                    "python3 -c \"from importlib.machinery import SourceFileLoader as L;m=L('d','/p/x').load_module();"
                    "print(m._kc('get','answer-key'))\""):
            with self.subTest(cmd=cmd):
                self.assert_refused("Bash", {"command": cmd})
        self.assert_allowed("Bash", {"command": "grep -n read_key plugins/datastash/scripts/datastash"}, self.project)

    def test_writing_a_script_that_reads_the_folder_is_refused(self):
        body = "import os\nprint(open(os.path.expanduser('~/.config/datastash/person.key')).read())\n"
        for name, ti in (("Write", {"file_path": os.path.join(self.project, "x.py"), "content": body}),
                         ("Edit", {"file_path": os.path.join(self.project, "x.py"), "old_string": "a", "new_string": body}),
                         ("MultiEdit", {"file_path": os.path.join(self.project, "x.py"),
                                        "edits": [{"old_string": "a", "new_string": body}]}),
                         ("Write", {"file_path": os.path.join(self.project, "y.sh"),
                                    "content": "cat ~/.con''fig/data''stash/main.json"})):
            with self.subTest(tool=name):
                self.assert_refused(name, ti, self.project)
        self.assert_allowed("Write", {"file_path": os.path.join(self.project, "z.sh"),
                                      "content": "tail ~/.config/datastash/state/agent-web-app.log\n"}, self.project)


class FolderSearches(KeyBase):
    """The review's case: naming only the folder (or a folder above it) and searching inside."""

    def test_grep_naming_only_the_folder_is_refused(self):
        for p in (DEFAULT, "~/.config/datastash", "$HOME/.config/datastash/", os.path.join(self.links, "conf-link"),
                  os.path.join(DEFAULT, "state"), os.path.join(DEFAULT, "pending")):
            with self.subTest(path=p):
                self.assert_refused("Grep", {"pattern": "dsak_", "path": p, "output_mode": "content"})
                self.assert_refused("Bash", {"command": f"grep -r dsak_ {p}"})
                self.assert_refused("Bash", {"command": f"ls {p}"})

    def test_a_search_of_a_folder_above_it_is_refused_in_the_file_tools_only(self):
        """PM ruling (review 1.0.4): Bash and Monitor have no folder-above check for "/" or a script's own walk, only a
        literal-name check. XLI-419 exposure #4 adds the named home folder and ~/.config (test below)."""
        for p in (HOME, "~", "~/.config", "/", os.path.dirname(DEFAULT)):
            with self.subTest(path=p):
                self.assert_refused("Grep", {"pattern": "dsak_", "path": p})
                self.assert_allowed("Bash", {"command": f"python3 -c \"import os;[print(r) for r in os.walk('{p}')]\""})
        for cmd in ("grep -r dsak_ /", "find / -type f -exec cat {} +", "tar czf /tmp/all.tgz /", "rsync -a / /tmp/copy"):
            with self.subTest(cmd=cmd):
                self.assert_allowed("Bash", {"command": cmd})
                self.assert_allowed("Monitor", {"command": cmd})

    def test_a_search_or_copy_of_home_or_config_by_name_is_refused_in_bash(self):
        """XLI-419 exposure #4: find, tar, cp -r, grep -r, rsync, zip, ls -R (and the like) over ~ or ~/.config."""
        for p in (HOME, "~", "~/", "$HOME", "${HOME}", "~/.config", "~/.config/", "$HOME/.config", os.path.dirname(DEFAULT)):
            for cmd in (f"grep -r dsak_ {p}", f"grep -R -l dsak_ {p}", f"grep --recursive x {p}", f"egrep -rn x {p}",
                        f"rg dsak_ {p}", f"rg --hidden dsak_ {p}", f"find {p} -type f -exec cat {{}} +", f"find {p} -name '*.key'", f"find {p} -maxdepth 3 -type f",
                        f"tar czf /tmp/all.tgz {p}", f"cp -r {p} /tmp/copy", f"cp -a {p} /tmp/copy", f"rsync -a {p} /tmp/copy",
                        f"zip -r /tmp/all.zip {p}", f"ls -R {p}", f"ls -laR {p}", f"cd /tmp && tar cf x.tar {p}",
                        f"git status; find {p} -type f | head", f"sudo find {p} -name x", f"echo hi && rg x {p}"):
                for name in ("Bash", "Monitor"):
                    with self.subTest(cmd=cmd, tool=name):
                        self.assert_refused(name, {"command": cmd}, self.project)

    def test_a_note_or_message_that_names_such_a_command_is_not_a_search(self):
        """XLI-419 exposure #4 keeps 1.0.36's fixes: text that only names a command is fine, and so is ordinary work."""
        for cmd in ("cat > notes.md <<'EOF'\nTo look, run find ~ -name x or grep -r foo ~/.config\nEOF",
                    "git commit -m \"docs: never run grep -r x ~ or tar czf a.tgz ~/.config\"",
                    "echo 'find ~ -type f is refused'", "ls ~", "ls -la ~/.config", "ls -l ~",
                    "grep -rn foo ~/code", "find ~/code -name '*.py'", "tar czf /tmp/a.tgz ~/code/web-app",
                    "cp -r ~/code/web-app /tmp/copy", "cp ~/notes.md /tmp/", "find . -name '*.md'", "grep -rn foo .",
                    "datastash status", "printf 'see ls -R ~ in the notes'\n", "find ~ -maxdepth 2 -name x",
                    "find ~/.config -maxdepth 1 -type d"):
            for name in ("Bash", "Monitor"):
                with self.subTest(cmd=cmd, tool=name):
                    self.assert_allowed(name, {"command": cmd}, self.project)
        self.assert_allowed("Write", {"file_path": os.path.join(self.project, "notes.md"),
                                      "content": "Run find ~ -name x, or grep -r foo ~/.config, or tar czf a.tgz ~.\n"},
                            self.project)

    def test_xli441_wider_forms_are_refused(self):
        """XLI-441 (1.0.40 review LOW #4): a cd into the home or ~/.config before a search of "." or a relative path, a
        folder above the home (never "/"), the command inside sh -c or eval, and a variable set in the same line."""
        above = os.path.dirname(HOME)
        for cmd in ("cd ~ && find .", "cd && grep -r dsak_", "cd $HOME; grep -rn dsak_ .", "cd ~/.config && ls -R",
                    "cd ~ && tar czf /tmp/a.tgz .", "cd ~ && cp -r .config /tmp/x", "(cd ~ && rg dsak_)",
                    "cd /tmp && cd ~ && find . -type f", "if true; then cd ~; fi; find .",
                    f"grep -r dsak_ {above}", f"find {above} -name '*.key'", f"tar czf /tmp/all.tgz {above}",
                    "bash -c 'find ~'", "sh -c \"grep -r dsak_ $HOME\"", "/bin/bash -lc 'tar czf /tmp/a.tgz ~/.config'",
                    "eval 'cp -r ~ /tmp/x'", "sudo bash -c 'find ~ -type f'",
                    "H=~; find $H", "H=$HOME; grep -r x ${H}", "export D=~/.config; tar czf /tmp/a.tgz $D",
                    "D=~/.config\nls -R $D"):
            for name in ("Bash", "Monitor"):
                with self.subTest(cmd=cmd, tool=name):
                    self.assert_refused(name, {"command": cmd}, self.project)

    def test_xli441_wider_forms_keep_ordinary_work(self):
        """XLI-441 keeps 1.0.36's fixes and ordinary work: a cd elsewhere, a shallow find, a message naming the forms."""
        for cmd in ("cd ~/code && find .", "cd ~/code/web-app && grep -rn foo .", "cd ~ && ls", "cd ~ && ls -la",
                    "cd ~ && find . -maxdepth 2 -name x", "cd \"my folder\" && find .", "cd - && find .",
                    "cd ~/code; cd web-app; grep -r foo", "find / -name x", "grep -r dsak_ /",
                    "bash -c 'find ~/code -name x'", "git commit -m \"never run bash -c 'find ~' or H=~; find $H\"",
                    "echo \"cd ~ && find .\"", "cat > n.md <<'EOF'\ncd ~ && find .\nbash -c 'find ~'\nEOF",
                    "H=~/code; find $H -name x", "X=1; grep -r foo .", "grep -r do ~/code"):
            for name in ("Bash", "Monitor"):
                with self.subTest(cmd=cmd, tool=name):
                    self.assert_allowed(name, {"command": cmd}, self.project)

    def test_a_search_with_no_folder_where_the_session_runs_above_it(self):
        self.assert_refused("Grep", {"pattern": "dsak_"})  # cwd HOME
        self.assert_refused("Grep", {"pattern": "dsak_"}, cwd=os.path.join(HOME, ".config"))
        for cmd in ("rg dsak_", "grep -r dsak_", "grep -rn dsak_ .", "ag dsak_", "git grep dsak_",
                    "python3 -c \"import pathlib;[print(p.read_text()) for p in pathlib.Path.home().rglob('*')]\""):
            with self.subTest(cmd=cmd):
                self.assert_allowed("Bash", {"command": cmd})  # no literal name: the accepted Bash limit

    def test_a_glob_that_spans_the_folder_is_refused(self):
        self.assert_refused("Glob", {"pattern": "**/*", "path": HOME})
        self.assert_refused("Glob", {"pattern": ".config/**"})
        self.assert_refused("Bash", {"command": "cat ~/.*/*/*"})
        self.assert_refused("Bash", {"command": "grep dsak_ ~/.config/*/*"})


class RepoWorkFromHome(KeyBase):
    """Review 1.0.4 HIGH: a session (or subagent) started in the home folder that cds into a repo is not refused."""

    def test_the_reviewers_examples_are_allowed(self):
        rel = "~/" + os.path.relpath(self.project, HOME)
        for repo in (rel, self.project):
            for cmd in (f'cd {repo} && git add -A && git commit -m "fix the parser"',
                        f"cd {repo} && git add -A",
                        f"cd {repo} && grep -rn foo .", f"cd {repo} && grep -rn foo",
                        f"cd {repo} && find . -name '*.py' -exec wc -l {{}} +",
                        f"cd {repo} && ls | xargs wc -l", f"cd {repo} && tar czf /tmp/a.tgz .",
                        f"cd {repo} && python3 -c \"import os;[print(r) for r in os.walk('.')]\"",
                        f"git -C {repo} add -A"):
                for name in ("Bash", "Monitor"):
                    with self.subTest(cmd=cmd, tool=name):
                        self.assert_allowed(name, {"command": cmd}, HOME)

    def test_literal_key_paths_in_bash_are_still_refused(self):
        rel = "~/" + os.path.relpath(self.project, HOME)
        up = os.path.relpath(DEFAULT, os.path.join(self.project, "src"))  # ../../.config/datastash
        for cmd in (f"cd {rel} && cat ~/.config/datastash/{KEY}", f"cd {rel} && cat $HOME/.config/datastash/person.key",
                    f"cd {rel} && cat ${{HOME}}/.config/datastash/login.refresh",
                    f"cd {rel} && cat {DEFAULT}/main.json", f"cd {rel}/src && cat {up}/{KEY}",
                    f"cd {rel} && cat ../.config/datastash/{KEY}", f"cd {rel} && cat ~/.CONFIG/DataStash/Person.KEY",
                    f"cd {rel} && grep -rn foo ~/.config/datastash", f"cd {rel} && tar czf /tmp/a.tgz ~/.config/datastash",
                    f"cd {rel} && ls ~/.config/datastash | xargs cat", f"cd {rel} && git add -A && cat agent-web-app.key",
                    "cd ~/.config/datastash && grep -rn foo ."):
            for name in ("Bash", "Monitor"):
                with self.subTest(cmd=cmd, tool=name):
                    self.assert_refused(name, {"command": cmd}, HOME)


class EverySession(KeyBase):
    """Through the real PreToolUse hook: the main session and a subagent, clean sessions off and on."""

    def test_the_hook_refuses_in_the_main_session_and_in_a_subagent(self):
        for clean in (False, True):
            ds.set_clean(commands=clean, no_code=False)
            for agent_id in (None, "a1b2c3d4"):
                for name, ti in (("Read", {"file_path": "~/.config/datastash/agent-web-app.key"}),
                                 ("Grep", {"pattern": "dsak_", "path": "~/.config/datastash"}),
                                 ("Glob", {"pattern": "*", "path": DEFAULT}),
                                 ("Bash", {"command": "grep -r dsak_ ~/.config/datastash"}),
                                 ("Monitor", {"command": "tail -f ~/.config/datastash/login.answer"}),
                                 ("mcp__files__read_file", {"path": "~/.config/datastash/login.refresh"})):
                    with self.subTest(clean=clean, subagent=bool(agent_id), tool=name):
                        out = self.hook(ds.cmd_agent_tool, self.call(name, ti, agent_id=agent_id))
                        self.assertTrue(self.key_denied(out), out)
                        self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"], ds.KEY_DENY)
                        self.assertNotIn("made-up", json.dumps(out))
        ds.set_clean(commands=False, no_code=False)

    def test_the_refusal_never_shows_a_key(self):
        out = self.hook(ds.cmd_agent_tool, self.call("Read", {"file_path": os.path.join(ds.CONF, "agent-keys-test.key")}))
        self.assertTrue(self.key_denied(out))
        self.assertNotIn(FAKE_AGENT_KEY, json.dumps(out))

    def test_only_before_the_call(self):
        for event in ("PostToolUse", "PostToolUseFailure"):
            inp = dict(self.call("Read", {"file_path": os.path.join(DEFAULT, KEY)}), hook_event_name=event)
            self.assertIsNone(ds.key_path_deny(inp))


class StillWorks(KeyBase):
    def test_ordinary_work_is_not_refused(self):
        p = self.project
        for name, ti, cwd in (("Read", {"file_path": os.path.join(p, "src", "app.py")}, p),
                              ("Read", {"file_path": "src/app.py"}, p),
                              ("Grep", {"pattern": "hi", "path": p}, p), ("Grep", {"pattern": "hi"}, p),
                              ("Glob", {"pattern": "**/*.py"}, p), ("Glob", {"pattern": "src/*.py", "path": p}, p),
                              ("Write", {"file_path": os.path.join(p, "notes.md"), "content": "x"}, p),
                              ("Bash", {"command": "git status && rg hi src && grep -rn hi ."}, p),
                              ("Bash", {"command": "find . -name '*.py' -exec wc -l {} +"}, p),
                              ("Bash", {"command": "ls ~ && ls -la ~/.config && find ~ -maxdepth 1 -name notes"}, p),
                              ("Bash", {"command": "cat ~/.bashrc; echo $HOME"}, p),
                              ("Bash", {"command": "datastash status && datastash api GET /x && datastash watch"}, p),
                              ("Bash", {"command": 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/datastash" status'}, p),
                              ("Bash", {"command": "datastash login --check"}, HOME),
                              ("Monitor", {"command": "datastash watch"}, p),
                              ("Agent", {"prompt": "read ~/.config/datastash/login.answer"}, p),
                              ("WebSearch", {"query": "datastash"}, p),
                              ("mcp__files__read_file", {"path": os.path.join(p, "src", "app.py")}, p)):
            with self.subTest(tool=name, ti=ti):
                self.assert_allowed(name, ti, cwd)

    def test_the_listener_logs_stay_readable(self):
        log = os.path.join(DEFAULT, "state", "agent-web-app.log")
        for name, ti in (("Read", {"file_path": log}), ("Read", {"file_path": "~/.config/datastash/state/agent-web-app.log"}),
                         ("Bash", {"command": "tail -n 50 ~/.config/datastash/state/agent-web-app.log"}),
                         ("Bash", {"command": f"cat {log}.1"}), ("Monitor", {"command": f"tail -f {log}"})):
            with self.subTest(tool=name, ti=ti):
                self.assert_allowed(name, ti)

    def test_a_log_name_that_is_a_symlink_to_a_key_is_refused(self):
        log = os.path.join(DEFAULT, "state", "agent-sneaky.log")
        os.symlink(os.path.join(DEFAULT, "login.answer"), log)
        self.addCleanup(os.replace, log, os.path.join(self.links, "moved-sneaky"))
        self.assert_refused("Read", {"file_path": log})
        self.assert_refused("Bash", {"command": f"cat {log}"})

    def test_the_datastash_connector_is_not_refused(self):
        ds.CLAUDE_USER_CONFIG = os.path.join(self.project, ".claude.json")
        self.addCleanup(setattr, ds, "CLAUDE_USER_CONFIG", self.saved_cfg)
        with open(ds.CLAUDE_USER_CONFIG, "w") as f:
            json.dump({"mcpServers": {"ds": {"type": "http", "url": "https://getdatastash.com/mcp"}}}, f)
        self.assert_allowed("mcp__ds__ledger_rows_get", {"tab": "notes about login.answer and ~/.config/datastash"})

    def setUp(self):
        super().setUp()
        self.saved_cfg = ds.CLAUDE_USER_CONFIG

    def test_the_plugins_own_scripts_still_read_the_key(self):
        """The hook stops tool calls only: the plugin's own reads (outside any tool call) still get the key."""
        self.assertEqual(ds._read_key_file(os.path.join(ds.CONF, "agent-keys-test.key"), r"ldgr_ag_[A-Za-z0-9_\-]+",
                                           "keys-test"), FAKE_AGENT_KEY)

    def test_a_folder_the_plugin_never_makes_inside_its_folder_is_not_refused(self):
        foreign = os.path.realpath(harness.tmpdir(self, prefix="proj-", dir=ds.CONF))  # how the other tests lay out
        self.assert_allowed("Bash", {"command": "git status && cat README.md && ls *.py"}, foreign)
        self.assert_allowed("Grep", {"pattern": "x"}, foreign)
        self.assert_refused("Bash", {"command": "cat ../agent-keys-test.key"}, foreign)
        self.assert_refused("Bash", {"command": "ls .."}, foreign)


# 1.0.9 review 1: the engine login's folder (~/.xlsx-for-ai: its config.json holds the engine key xlsx-test reads) is
# guarded like the datastash folder, all of it, in every path form
ENGINE = os.path.join(HOME, ".xlsx-for-ai")
FAKE_ENGINE_KEY = "xfa_" + "e" * 30  # made up, never a real key
ENGINE_FORMS = (
    ("absolute", os.path.join(ENGINE, "config.json"), None),
    ("the folder itself", ENGINE, None),
    ("relative to the session's folder", os.path.join(".xlsx-for-ai", "config.json"), None),
    ("relative with ..", os.path.join("..", "..", ".xlsx-for-ai", "config.json"), "PROJECT/src"),
    ("tilde", "~/.xlsx-for-ai/config.json", None),
    ("tilde folder", "~/.xlsx-for-ai/", None),
    ("$HOME", "$HOME/.xlsx-for-ai/config.json", None),
    ("${HOME}", "${HOME}/.xlsx-for-ai/config.json", None),
    ("glob on the folder", "~/.xlsx-*/config.json", None),
    ("glob on the file", "~/.xlsx-for-ai/*", None),
    ("glob on both", "~/.x?sx-for-a*/c*", None),
    ("brace", "~/.{xlsx-for-ai,other}/config.json", None),
    ("symlink to the folder", "LINKS/engine-link/config.json", None),
    ("symlink to the file", "LINKS/engine-file", None),
    ("upper case", "~/.XLSX-For-AI/CONFIG.JSON", None),
    ("another file in there", "~/.xlsx-for-ai/config.json.tmp-123", None),
)


class EngineLoginFolder(KeyBase):
    def setUp(self):
        super().setUp()
        put(os.path.join(ENGINE, "config.json"), json.dumps({"client_id": "c-1", "api_key": FAKE_ENGINE_KEY}))
        put(os.path.join(ENGINE, "config.json.tmp-123"), "made-up\n")
        os.symlink(ENGINE, os.path.join(self.links, "engine-link"))
        os.symlink(os.path.join(ENGINE, "config.json"), os.path.join(self.links, "engine-file"))
        self.addCleanup(os.replace, ENGINE, os.path.join(self.links, "moved-engine"))

    def form(self, raw, cwd):
        return raw.replace("LINKS", self.links), (cwd or HOME).replace("PROJECT", self.project)

    def test_every_tool_refuses_every_path_form(self):
        for label, raw, cwd in ENGINE_FORMS:
            p, c = self.form(raw, cwd)
            for name, ti in (("Read", {"file_path": p}), ("Write", {"file_path": p, "content": "x"}),
                             ("Edit", {"file_path": p, "old_string": "a", "new_string": "b"}),
                             ("MultiEdit", {"file_path": p, "edits": []}),
                             ("NotebookEdit", {"notebook_path": p, "new_source": "x"}),
                             ("Grep", {"pattern": "api_key", "path": p}), ("Glob", {"pattern": "*", "path": p}),
                             ("Bash", {"command": f"cat {p}"}), ("Bash", {"command": f'head -c 9 "{p}"'}),
                             ("Bash", {"command": f"cp {p} /tmp/x"}), ("Bash", {"command": f"jq -r .api_key {p}"}),
                             ("Monitor", {"command": f"tail -f {p}"}), ("mcp__files__read_file", {"path": p}),
                             ("SomeNewTool", {"target": {"file": p}})):
                with self.subTest(form=label, tool=name):
                    self.assert_refused(name, ti, c)

    def test_a_cd_a_split_path_and_a_script_are_refused(self):
        for cmd in ("cd ~ && cat .xlsx-for-ai/config.json", "cd ~/.xlsx-for-ai; cat config.json",
                    "cd ~/.xlsx-for-ai && grep -rn api_key .", "ls ~/.xlsx-for-ai",
                    "cat ~/.xlsx''-for-ai/config.json", 'cat ~/".xlsx"-for-ai/config.json', "cat ~/.xl\\sx-for-ai/config.json",
                    "python3 -c \"import os;print(open(os.path.expanduser('~/.xlsx'+'-for-ai/config.json')).read())\"",
                    "node -e \"fs.readFileSync(HOME+'/.xlsx-'+'for-ai/config.json')\""):
            with self.subTest(cmd=cmd):
                self.assert_refused("Bash", {"command": cmd}, self.project)
        body = "import os\nprint(open(os.path.expanduser('~/.xlsx-for-ai/config.json')).read())\n"
        for name, ti in (("Write", {"file_path": os.path.join(self.project, "x.py"), "content": body}),
                         ("Edit", {"file_path": os.path.join(self.project, "x.py"), "old_string": "a", "new_string": body})):
            with self.subTest(tool=name):
                self.assert_refused(name, ti, self.project)

    def test_a_search_of_a_folder_holding_it_is_refused_in_the_file_tools(self):
        self.assert_refused("Grep", {"pattern": "api_key", "path": HOME})
        self.assert_refused("Glob", {"pattern": ".x*/**", "path": HOME})
        self.assert_refused("Bash", {"command": "cat ~/.*/config.json"})

    def test_the_hook_refuses_in_the_main_session_and_in_a_subagent(self):
        for clean in (False, True):
            ds.set_clean(commands=clean, no_code=False)
            for agent_id in (None, "a1b2c3d4"):
                for name, ti in (("Read", {"file_path": "~/.xlsx-for-ai/config.json"}),
                                 ("Bash", {"command": "cat $HOME/.xlsx-for-ai/config.json"}),
                                 ("Grep", {"pattern": "api_key", "path": "~/.xlsx-for-ai"})):
                    with self.subTest(clean=clean, subagent=bool(agent_id), tool=name):
                        out = self.hook(ds.cmd_agent_tool, self.call(name, ti, agent_id=agent_id))
                        self.assertTrue(self.key_denied(out), out)
                        self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"], ds.KEY_DENY)
                        self.assertNotIn(FAKE_ENGINE_KEY, json.dumps(out))
        ds.set_clean(commands=False, no_code=False)

    def test_ordinary_work_and_the_engine_name_are_not_refused(self):
        p = self.project
        for name, ti in (("Bash", {"command": "npx xlsx-for-ai login"}),
                         ("Bash", {"command": "curl -s https://api.xlsx-for-ai.dev/healthz"}),
                         ("Bash", {"command": "datastash xlsx-test out.xlsx --set invoice"}),
                         ("Bash", {"command": "git status && grep -rn xlsx-for-ai ."}),
                         ("Read", {"file_path": os.path.join(p, "src", "app.py")}),
                         ("Write", {"file_path": os.path.join(p, "ci.yml"), "content": "run: npx xlsx-for-ai login\n"})):
            with self.subTest(tool=name, ti=ti):
                self.assert_allowed(name, ti, p)

    def test_a_bare_wildcard_in_home_is_not_a_read_of_it_unless_dotglob_is_on(self):
        for cmd in ("ls *", "wc -l * | tail -1", "cat ~/*/config.json"):
            with self.subTest(cmd=cmd):
                self.assert_allowed("Bash", {"command": cmd}, HOME)
        for cmd in ("shopt -s dotglob; cat */config.json", "shopt -s dotglob && cat ~/*/*"):
            with self.subTest(cmd=cmd):
                self.assert_refused("Bash", {"command": cmd}, HOME)

    def test_the_plugins_own_reader_still_gets_the_key(self):
        old = os.environ.pop("XFA_API_KEY", None)
        self.addCleanup(lambda: old is None or os.environ.__setitem__("XFA_API_KEY", old))
        self.assertEqual(ds.xt_engine_key(), (FAKE_ENGINE_KEY, "login"))


class Xli422EpmCommands(KeyBase):
    """XLI-422 (an EPM ask, 11-26): the guard refused three builder commands that touch neither key folder. Each shape
    below was refused before the fix (a JSDoc "/**" read as a glob of every folder; "agent-..." and a later ".key" read
    as one file name across the whole here-doc or command; a ** under the home folder read as crossing ~/.config). Bash
    and Monitor now read globs as the shell expands them, and only a word, never a whole command, can be a key file name.
    Every true block still holds."""

    def setUp(self):
        super().setUp()
        self.wt = os.path.join(HOME, "e", "wt", "xls-10486")  # an engine worktree, like EPM's (made-up names)
        put(os.path.join(self.wt, ".prepush", "pr8.log"), " Test Files  1 passed (1)\n")
        self.scratch = os.path.join(HOME, "e", "scratch", "ci-times")
        put(os.path.join(self.scratch, "ci-1.log"), " test/cases/a.test.ts (3 tests) 120ms\n")

    def commands(self):
        heredoc = (f"cat > {self.wt}/test/cases/agent-key-shape.test.ts <<'EOF'\n"
                   "/** Made-up test: the key shape (xlsx-for-ai-server). */\n"
                   "import {{ describe, it, expect }} from 'vitest';\n"
                   "describe('agent-key shape', () => {{\n"
                   "  it('reads the answer', () => {{ const r = load(); expect(r.key).toBe(1); expect(r.answer).toBe(2); }});\n"
                   "}});\nEOF").replace("{{", "{").replace("}}", "}")
        until = (f"cd {self.wt}/.prepush && until grep -qE 'agent-notes|Test Files .*(passed|failed)' pr8.log; do "
                 "sleep 15; done; grep -c 'expect(r.key).toBe' pr8.log")
        rank = ("grep -hoE '[^ ]+[.]test[.]ts [(][0-9]+ tests?[)] [0-9]+ms' ~/e/**/ci-*.log $HOME/**/ci-*.log "
                "| sort -t' ' -k4 -rn | head -20")
        return (("here-doc", heredoc, self.wt), ("until-grep", until, self.wt), ("rank", rank, self.scratch))

    def test_epms_three_commands_pass(self):
        for label, cmd, cwd in self.commands():
            for name in ("Bash", "Monitor"):
                with self.subTest(cmd=label, tool=name):
                    self.assert_allowed(name, {"command": cmd}, cwd)

    def test_they_pass_through_the_real_hook_in_a_subagent_and_the_main_session(self):
        for label, cmd, cwd in self.commands():
            for agent in (None, "agent-xli422"):
                with self.subTest(cmd=label, agent=agent):
                    out = ds.key_path_deny(self.call("Bash", {"command": cmd}, cwd, agent_id=agent))
                    self.assertIsNone(out)

    def test_globs_read_as_the_shell_expands_them(self):
        for cmd in ("cat /** | head", "grep -rn foo /**/x", "ls ~/* ~/*/*"):
            with self.subTest(cmd=cmd):
                self.assert_allowed("Bash", {"command": cmd}, self.wt)

    def test_the_real_blocks_still_hold(self):
        k = "agent-web-app.key"
        for cmd in (f"cat ~/.config/datastash/{k}", f"cat $HOME/.config/datastash/{k}", f"cat ${{HOME}}/.config/datastash/{k}",
                    f"cat {DEFAULT}/{k}", "cp ~/.config/datastash/person.key /tmp/x", f"cp -r {DEFAULT} /tmp/copy",
                    "grep -r dsak_ ~/.config/datastash", "grep -rn x $HOME/.config/datastash/", "ls ~/.config/datastash",
                    "cat ~/.config/datastash/*", "cat ~/.config/data*/*", "cat ~/.c*/d?tastash/*", "cat ~/.*/*/*",
                    "cat ~/.config/**", "cat ~/*/datastash/*", "cat ~/**/datastash/person.key", "grep dsak_ ~/.config/*/*",
                    "cat ~/.xlsx-for-ai/config.json", "cat ~/.xlsx*/*", "cat ~/**/.xlsx-for-ai/config.json",
                    "shopt -s globstar; cat ~/**", "shopt -s dotglob; cat ~/*/*/*", "setopt glob_dots; cat ~/*/*/*",
                    "cat ~/.config/data''stash/main.json", "python3 -c \"open('~/.con' + 'fig/data' + 'stash/x')\"",
                    f"cd {self.wt} && cat agent-web-app.key", f"cd {self.wt} && cat ../../../.config/datastash/main.json",
                    "cat > /tmp/x.sh <<'EOF'\ncat ~/.config/datastash/person.key\nEOF"):
            for name in ("Bash", "Monitor"):
                with self.subTest(cmd=cmd, tool=name):
                    self.assert_refused(name, {"command": cmd}, self.wt)


if __name__ == "__main__":
    unittest.main()
