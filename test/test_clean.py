#!/usr/bin/env python3
"""Offline tests for XLI-386 (plugin 0.8.0): Clean sessions.
- Two per-person choices, each on or off by itself, kept with this computer's other per-person settings (so they hold
  for all the person's agents here): "commands through subagents" (default on) and "no code in sessions" (default off).
- The session start gives the AI the rules only for what is on.
- XLI-396 (owner 11-23): a block, not a nudge. While either choice is on, the main session (the hook input has no
  agent_id; a subagent's has one) runs only the listed tools that can't change a file or run code and the datastash
  connector's read-only tools (deny by default: any other tool, an unknown one or an MCP write, is refused), and every
  command tool (Bash, Monitor, PowerShell, any "command") only as one short read-only lookup, with one fixed line. A subagent is never refused. "turn off clean sessions" ends it all.
- The person's typed "no code in sessions", "turn off clean sessions" and "turn on clean sessions" change the choices;
  the agent confirms in one line.
No network, no key; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_clean.py"""
import json
import os
import sys
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import Base, ds  # noqa: E402  (the same harness, plugin copy and fixtures)

SKILL = os.path.join(HERE, "..", "plugins", "datastash", "skills", "datastash-agent", "SKILL.md")
BLOCK = "Clean sessions is on: hand this to a subagent (Agent tool), then report the result in plain words."
SECRET_CMD = "curl -H 'Authorization: Bearer sk-secret-value' https://example.invalid/x"


def bash(sid="sess1", event="PreToolUse", agent_id=None, cwd=None, cmd=SECRET_CMD, tool="Bash", tool_input=None):
    v = {"session_id": sid, "hook_event_name": event, "tool_name": tool,
         "tool_input": tool_input if tool_input is not None else {"command": cmd},
         "tool_use_id": "toolu_1", "cwd": cwd, "transcript_path": "/nowhere/" + sid + ".jsonl"}
    if agent_id:
        v["agent_id"] = agent_id
        v["agent_type"] = "general-purpose"
    return v


def ctx_of(out):
    return ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""


class Settings(Base):
    def test_defaults_commands_on_code_off(self):
        self.assertEqual(ds.clean_settings(), {"commands": True, "no_code": False})

    def test_each_choice_is_set_by_itself_and_kept_with_the_per_person_settings(self):
        ds.set_clean(no_code=True)
        self.assertEqual(ds.clean_settings(), {"commands": True, "no_code": True})
        ds.set_clean(commands=False)
        self.assertEqual(ds.clean_settings(), {"commands": False, "no_code": True})
        with open(ds.CONSENT_PATH) as f:
            self.assertEqual(json.load(f)["clean"]["commands"], False)

    def test_other_per_person_writes_keep_the_choices(self):
        ds.set_clean(commands=False, no_code=True)
        ds.record_consent([self.folder], [])
        ds.record_left_off([self.folder])
        ds.record_keep_all(False, "typed")
        self.assertEqual(ds.clean_settings(), {"commands": False, "no_code": True})

    def test_a_broken_value_falls_back_to_the_defaults(self):
        ds.write_private(ds.CONSENT_PATH, json.dumps({"clean": {"commands": "yes", "no_code": 1}}))
        self.assertEqual(ds.clean_settings(), {"commands": True, "no_code": False})


class RulesText(Base):
    def test_only_what_is_on_is_shown(self):
        on = ds.clean_session_text()
        self.assertIn("subagent", on)
        self.assertIn("plain words", on)
        self.assertNotIn("code blocks", on)
        ds.set_clean(no_code=True)
        both = ds.clean_session_text()
        self.assertIn("code blocks", both)
        self.assertIn("shell commands", both)
        ds.set_clean(commands=False)
        code_only = ds.clean_session_text()
        self.assertIn("code blocks", code_only)
        self.assertNotIn("shell commands", code_only)
        ds.set_clean(no_code=False)
        self.assertEqual(ds.clean_session_text(), "")

    def test_wording_states_a_choice_never_a_better_way(self):
        ds.set_clean(no_code=True)
        text = ds.clean_session_text().lower()
        for word in ("cleaner", "best practice", "better"):
            self.assertNotIn(word, text)

    def test_session_start_carries_the_rules_for_an_agent_session(self):
        out = self.hook(ds.cmd_check, {"session_id": "sess1", "hook_event_name": "SessionStart", "cwd": self.folder})
        self.assertIn(ds.clean_session_text(), ctx_of(out))
        ds.set_clean(commands=False)
        out = self.hook(ds.cmd_check, {"session_id": "sess1", "hook_event_name": "SessionStart", "cwd": self.folder})
        self.assertNotIn(ds.CLEAN_NOTE, ctx_of(out))


WRITE_TOOLS = {"Edit": {"file_path": "/x/a.py", "old_string": "a", "new_string": "b"},
               "Write": {"file_path": "/x/a.py", "content": "print(1)"},
               "MultiEdit": {"file_path": "/x/a.py", "edits": []},
               "NotebookEdit": {"notebook_path": "/x/a.ipynb", "new_source": "x"}}
READ_ONLY = ("ls", "ls -la src", "cat README.md", "head -n 20 a.py", "tail -n 5 log.txt", "grep -rn 'a|b' src",
             "wc -l a.py", "git status", "git log --oneline -5", "git diff HEAD~1", "git show HEAD", "gh pr view 12",
             "curl -s https://example.invalid/health", "curl -fsS https://example.invalid/x",
             "datastash status --all", "datastash main", "datastash check", "datastash read person",
             "datastash api GET /api/v1/agent/rows", "datastash api get '/api/v1/agent/inbox?limit=5&x=1'",
             '"${CLAUDE_PLUGIN_ROOT}/scripts/datastash" status --details 3', "grep -r --include='*.py' foo .",
             "grep -rn --include='*.md' 'a b' .", "git log --format='%h %s' -3", "ls 'src/*.py'", "tail -n +5 log")
WRITING = ("rm -rf x", "touch a", "ls > out.txt", "cat a >> b", "ls; rm x", "ls && touch a", "ls || touch a",
           "cat a | tee b", "echo hi", "python3 x.py", "npm test", "git commit -m x", "git push", "git checkout x",
           "git diff --output=x", "git -C /x status", "gh pr merge 1", "gh pr view 1 --web", "curl -X POST https://e.x/",
           "curl -s -o f https://e.x/", "curl -s -d a=1 https://e.x/", "curl https://e.x/", "tail -f log",
           "tail -fn 5 log", "ls $(touch x)", "ls `touch x`", "cat <(touch x)", "ls\ntouch x", "ls &", "(ls)",
           "/bin/ls", "FOO=1 ls", "sed -i s/a/b/ f", "find . -delete", "printf '%s' x | sh", "printf '%s' x | tee y",
           "cat 'unclosed", "", SECRET_CMD,
           # review 1 (be66261): "#" is no comment to bash inside a word; datastash writes; abbreviated long options
           "ls x#;touch F", "cat a#|sh", "ls #", "datastash api POST /api/v1/agent/x", "datastash api GET /x '{}'",
           "datastash status-set -", "printf '%s' '{\"doing\": \"x; y\"}' | datastash status-set -",
           "datastash send a", "datastash where x", "tail --fol x", "tail --f x", "tail --follow=name x", "tail -F x",
           "tail +1f x", "tail -1f x", "git status --porcelai", "grep --includ=x y", "git log --outp=F")


def denied(out):
    hs = (out or {}).get("hookSpecificOutput") or {}
    return hs.get("permissionDecision") == "deny" and hs.get("permissionDecisionReason") == BLOCK


class Enforce(Base):
    def test_main_session_write_tools_are_refused_with_the_one_line(self):
        for tool, ti in WRITE_TOOLS.items():
            out = self.hook(ds.cmd_agent_tool, bash(tool=tool, tool_input=ti, cwd=self.folder))
            self.assertTrue(denied(out), tool)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"], BLOCK)
            self.assertNotIn("a.py", json.dumps(out))

    def test_main_session_writing_bash_is_refused_and_never_repeated(self):
        for cmd in WRITING:
            out = self.hook(ds.cmd_agent_tool, bash(cmd=cmd, cwd=self.folder))
            self.assertTrue(denied(out), repr(cmd))
        out = self.hook(ds.cmd_agent_tool, bash(cwd=self.folder))
        self.assertNotIn("sk-secret-value", json.dumps(out))
        self.assertNotIn("example.invalid", json.dumps(out))

    def test_main_session_read_only_lookups_run(self):
        for cmd in READ_ONLY:
            self.assertTrue(ds.clean_lookup_ok(cmd), repr(cmd))
            self.assertFalse(denied(self.hook(ds.cmd_agent_tool, bash(cmd=cmd, cwd=self.folder))), repr(cmd))

    def test_a_background_lookup_is_refused(self):
        out = self.hook(ds.cmd_agent_tool, bash(tool_input={"command": "ls", "run_in_background": True},
                                                cwd=self.folder))
        self.assertTrue(denied(out))

    def test_a_subagent_is_never_refused(self):
        for cmd in WRITING:
            self.assertFalse(denied(self.hook(ds.cmd_agent_tool, bash(cmd=cmd, agent_id="a1b2c3", cwd=self.folder))),
                             repr(cmd))
        for tool, ti in WRITE_TOOLS.items():
            out = self.hook(ds.cmd_agent_tool, bash(tool=tool, tool_input=ti, agent_id="a1b2c3", cwd=self.folder))
            self.assertFalse(denied(out), tool)

    def test_other_tools_and_events_are_never_refused(self):
        for inp in (bash(cmd="rm x", event="PostToolUse", cwd=self.folder),
                    bash(cmd="rm x", event="PostToolUseFailure", cwd=self.folder),
                    bash(tool="Read", tool_input={"file_path": "/x/a.py"}, cwd=self.folder),
                    bash(tool="Grep", tool_input={"pattern": "x"}, cwd=self.folder),
                    bash(tool="Agent", tool_input={"prompt": "do it"}, cwd=self.folder)):
            self.assertFalse(denied(self.hook(ds.cmd_agent_tool, inp)), inp["tool_name"] + inp["hook_event_name"])

    def test_no_code_in_sessions_alone_still_refuses_and_also_refuses_lookups_that_show_code(self):
        ds.set_clean(commands=False, no_code=True)
        self.assertTrue(denied(self.hook(ds.cmd_agent_tool, bash(cmd="touch a", cwd=self.folder))))
        self.assertTrue(denied(self.hook(ds.cmd_agent_tool, bash(tool="Write", tool_input=WRITE_TOOLS["Write"],
                                                                 cwd=self.folder))))
        for cmd in ("cat a.py", "head a.py", "tail a.py", "grep -n x a.py", "git diff", "git show HEAD",
                    "git log -p", "git log --patch"):
            self.assertTrue(denied(self.hook(ds.cmd_agent_tool, bash(cmd=cmd, cwd=self.folder))), cmd)
        for cmd in ("ls", "wc -l a.py", "git status", "git log --oneline", "gh pr view 3", "datastash status --all"):
            self.assertFalse(denied(self.hook(ds.cmd_agent_tool, bash(cmd=cmd, cwd=self.folder))), cmd)

    def test_turn_off_clean_sessions_turns_it_all_off(self):
        self.hook(ds.cmd_prompt, {"session_id": "sess1", "hook_event_name": "UserPromptSubmit",
                                  "prompt": "turn off clean sessions", "cwd": self.folder,
                                  "prompt_id": str(uuid.uuid4()), "transcript_path": self.transcript("sess1")})
        for cmd in WRITING:
            self.assertFalse(denied(self.hook(ds.cmd_agent_tool, bash(cmd=cmd, cwd=self.folder))), repr(cmd))
        for tool, ti in WRITE_TOOLS.items():
            self.assertFalse(denied(self.hook(ds.cmd_agent_tool, bash(tool=tool, tool_input=ti, cwd=self.folder))))
        ds.set_clean(commands=True)  # and back on
        self.assertTrue(denied(self.hook(ds.cmd_agent_tool, bash(cmd="touch a", cwd=self.folder))))

    def test_the_persons_datastash_main_session_keeps_its_own_rules(self):
        ds.ensure_dirs()
        ds.write_private(ds._main_path("sess1"), json.dumps({"sid": "sess1"}))
        self.assertFalse(denied(self.hook(ds.cmd_agent_tool, bash(cmd="touch a", cwd=self.folder))))

    def test_no_new_state_files(self):
        before = sorted(os.listdir(ds.STATE)) if os.path.isdir(ds.STATE) else []
        for _ in range(3):
            self.hook(ds.cmd_agent_tool, bash(cmd="touch a", cwd=self.folder))
        after = sorted(os.listdir(ds.STATE)) if os.path.isdir(ds.STATE) else []
        self.assertEqual([x for x in after if x.startswith("clean")], [x for x in before if x.startswith("clean")])


# Review 1 (be66261) HIGH: tricks that make bash run "touch F". Each is refused, and each is also run in a real bash in a
# throwaway folder only when the check lets it through, so touch can never run; the live ones are run raw once too, to
# prove they are real attacks (F appears) and that the check, not luck, stops them.
BYPASS = (
    ("ls x#;touch F", True), ("cat a#|sh", True), ("ls x #; touch F", False), ("ls;touch F", True),
    ("ls&&touch F", True), ("ls nope||touch F", True), ("ls|touch F", True), ("ls&touch F", True),
    ("ls${IFS};touch${IFS}F", True), ("ls 'x';touch F", True), ('ls "x";touch F', True), ('ls "$(touch F)"', True),
    ("ls `touch F`", True), ("ls x\\\n;touch F", True), ("ls x\n touch F", True), ("ls $'\\x3b'touch F", False),
    ("ls x\\;touch F", False), ("ls x\u037etouch F", False), ("ls x\uff1btouch F", False), ("ls x\u2028touch F", False),
    ("ls\u00a0x", False), ("ls x\ttouch F", False), ("ls x\rtouch F", False), ("bash -c 'touch F'", True),
    ("sh -c 'touch F'", True), ("eval touch F", True), ("env touch F", True), ("xargs -a L touch", True),
    ("find . -name a -exec touch F \\;", True), ("find . -name a -exec touch F {} +", True),
    ("git -c alias.x='!touch F' x", True), ("git -c core.fsmonitor='touch F' status", True),
    ("git log --output=F", True), ("git log --outp=F", True), ("git log '--output=F'", True),
    ('git log "--output"=F', True), ("git log --out''put=F", True), ("git diff --output=F HEAD", True),
    ("git show --output=F", True), ("cat a#|sh #", True), ("ls x#|sh", False), ("tail +1f a", False),
    ("ls --hide=x#;touch F", True), ("grep -r x . --exclude=x#;touch F", True), ("ls x# ;touch F", True),
    ("ls ~/x#;touch F", True), ("ls x#&touch F", True), ("ls x#\ntouch F", True), ("ls x\x00;touch F", False),
    ("datastash status#;touch F", True), ("datastash api GET /x#;touch F", True),
)
LOCAL_LOOKUPS = ("ls", "ls -la", "cat a", "head -n 1 a", "tail -n 1 a", "tail -n +1 a", "wc -l a", "grep -rn touch .",
                 "grep -r --include='*.py' touch .", "grep -r --include='*' touch .", "git status", "git log --oneline -1",
                 "git log --format='%h %s' -1", "git diff HEAD", "git show --stat HEAD", "ls './*'", "cat 'a'")


class Bypass(Base):
    def shell_folder(self):
        import shutil
        import subprocess
        from harness import tmpdir
        d = tmpdir(self, prefix="clean-bypass-")
        with open(os.path.join(d, "a"), "w") as f:
            f.write("touch F\n")
        with open(os.path.join(d, "a#"), "w") as f:
            f.write("touch F\n")
        with open(os.path.join(d, "L"), "w") as f:
            f.write("F\n")
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x.invalid",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x.invalid")
        git = shutil.which("git")
        if git:
            for args in (["init", "-q"], ["add", "a", "L"], ["commit", "-qm", "x"]):
                subprocess.run([git, *args], cwd=d, env=env, capture_output=True, timeout=20)
        return d, env

    def run_bash(self, d, env, cmd):
        import subprocess
        if "\x00" in cmd:
            return
        subprocess.run(["bash", "-c", cmd], cwd=d, env=env, capture_output=True, timeout=10, stdin=subprocess.DEVNULL)

    def test_no_trick_lets_touch_run(self):
        d, env = self.shell_folder()
        f = os.path.join(d, "F")
        for cmd, _live in BYPASS:
            for no_code in (False, True):
                self.assertFalse(ds.clean_lookup_ok(cmd, no_code, cwd=d), repr(cmd))
            self.assertTrue(denied(self.hook(ds.cmd_agent_tool, bash(cmd=cmd, cwd=d))), repr(cmd))
            if ds.clean_lookup_ok(cmd, cwd=d):  # never: run only what the check lets through
                self.run_bash(d, env, cmd)
            self.assertFalse(os.path.exists(f), repr(cmd))

    def test_the_live_tricks_are_real(self):
        import shutil
        if not shutil.which("git"):
            self.skipTest("no git")
        d, env = self.shell_folder()
        f, dead = os.path.join(d, "F"), []
        for cmd, live in BYPASS:
            if not live or cmd.startswith("datastash") or "touch" not in cmd and "|sh" not in cmd:
                continue
            if os.path.exists(f):
                os.replace(f, os.path.join(d, "F-old"))
            self.run_bash(d, env, cmd)
            if not os.path.exists(f):
                dead.append(cmd)
        self.assertEqual(dead, [], "not live attacks in bash")

    def test_allowed_lookups_run_in_bash_and_never_write(self):
        d, env = self.shell_folder()
        for cmd in LOCAL_LOOKUPS:
            self.assertTrue(ds.clean_lookup_ok(cmd, cwd=d), repr(cmd))
            before = sorted(os.listdir(d))
            self.run_bash(d, env, cmd)
            self.assertEqual(sorted(os.listdir(d)), before, repr(cmd))
            self.assertFalse(os.path.exists(os.path.join(d, "F")), repr(cmd))

    def test_a_planted_file_name_can_never_become_an_option(self):
        d, env = self.shell_folder()
        for name in ("--output=F", "-f", "+1f", "--follow"):
            with open(os.path.join(d, name), "w") as fh:
                fh.write("x\n")
        for cmd in ("ls *", "git log *", "tail *", "cat ?f", "ls -*", "tail +*", "git log --*", "ls ./*"):
            if ds.clean_lookup_ok(cmd, cwd=d):
                self.run_bash(d, env, cmd)
            self.assertFalse(os.path.exists(os.path.join(d, "F")), repr(cmd))
        for cmd in ("ls -*", "tail +*", "git log --*", "tail *f", "ls ?*"):
            self.assertFalse(ds.clean_lookup_ok(cmd, cwd=d), repr(cmd))

    def test_endless_and_secret_files_are_refused(self):
        for cmd in ("cat /dev/zero", "head /dev/urandom", "tail -n 1 /proc/self/environ", "grep -r x /proc",
                    "wc -c /dev/stdin", "cat ../../../../../../../../dev/zero", "tail -f a", "tail --follow a",
                    "tail --retry a", "tail --pid=1 a", "tail -s 1 a"):
            self.assertFalse(ds.clean_lookup_ok(cmd, cwd=self.folder), repr(cmd))

    def test_only_listed_long_options_spelled_in_full(self):
        for cmd in ("tail --fol a", "tail --follo a", "tail --f a", "git log --out=x", "git log --output-directory=x",
                    "git diff --ext-diff", "git diff --textconv", "git log -c", "gh pr view 1 --we",
                    "grep --exclude-from=x y", "grep -f x y", "ls --color", "git status --no-renames=x",
                    "wc --files0-from=x", "git log --no-walk", "git log --exec=x", "cat --number=3 a"):
            self.assertFalse(ds.clean_lookup_ok(cmd), repr(cmd))
        for cmd in ("tail --lines=3 a", "git log --stat", "grep --count x a", "ls --all", "git status --porcelain=v2",
                    "gh pr view 3 --json title,state --jq .title", "wc --lines a"):
            self.assertTrue(ds.clean_lookup_ok(cmd, cwd=self.folder), repr(cmd))

    def test_datastash_reads_only(self):
        for cmd in ("datastash status", "datastash status --more 2", "datastash check", "datastash read person",
                    "datastash api GET /api/v1/agent/rows", "datastash help", "datastash main", "datastash login --check"):
            self.assertTrue(ds.clean_lookup_ok(cmd), repr(cmd))
        for cmd in ("datastash api POST /api/v1/agent/rows", "datastash api PUT /x", "datastash api PATCH /x",
                    "datastash api DELETE /x", "datastash api GET /x -", "datastash api GET /x @f",
                    "datastash status-set -", "datastash status-send", "datastash send a", "datastash reply 1",
                    "datastash link a --workbook x", "datastash off a", "datastash keep --on", "datastash fetch a -",
                    "datastash logout", "datastash ci-report x.xml", "datastash fingerprint a", "datastash where x",
                    "datastash file a", "datastash status --all --install x", "python3 datastash status",
                    "./datastash status", "datastash filter x", "datastash self-update", "datastash token --header"):
            self.assertFalse(ds.clean_lookup_ok(cmd), repr(cmd))

    def test_a_check_that_breaks_refuses(self):
        saved = ds._clean_lookup_ok
        ds._clean_lookup_ok = lambda *a: 1 / 0
        try:
            self.assertFalse(ds.clean_lookup_ok("ls"))
            self.assertTrue(denied(self.hook(ds.cmd_agent_tool, bash(cmd="ls", cwd=self.folder))))
        finally:
            ds._clean_lookup_ok = saved


# Review 2 (0f28b06) HIGH: a deny-list of tools let Monitor (and PowerShell on Windows) run any command. Now deny by
# default: only the listed tools that can't change a file or run code, the datastash connector's read-only tools, and
# the one command check for every tool that runs a command.
DS_MCP = "mcp__plugin_datastash_datastash__"
ALLOWED_INPUTS = {"Read": {"file_path": "/x/a.py"}, "Grep": {"pattern": "x"}, "Glob": {"pattern": "*.py"},
                  "Agent": {"prompt": "do it", "description": "x"}, "Task": {"prompt": "do it"},
                  "SendMessage": {"to": "a", "message": "hi"}, "TaskStop": {"task_id": "t1"},
                  "ToolSearch": {"query": "select:Monitor"}, "AskUserQuestion": {"questions": []},
                  "ListAgents": {}, "ReadNotifications": {}, "WebFetch": {"url": "https://example.invalid/", "prompt": "x"},
                  "WebSearch": {"query": "x"}, "EnterPlanMode": {}, "TodoWrite": {"todos": []},
                  "TaskCreate": {"subject": "x", "description": "x"}, "TaskUpdate": {"taskId": "1", "status": "completed"},
                  "TaskList": {}, "TaskGet": {"taskId": "1"}, "TaskOutput": {"task_id": "t1", "block": False},
                  "ListMcpResourcesTool": {}}
GLOBS = ("cat /pro?/self/environ", "cat /d?v/zero", "cat /proc/sel?/environ", "ls *", "ls ./*", "cat ./a*",
         "cat [a]", "cat /[p]roc/self/environ", "ls {a,b}", "cat /{proc,x}/self/environ", "ls a{,}",
         "grep -r --include=*.py x .", "wc -l a*", "git log -- *", "curl -s 'http://x/[1-3]'",
         "curl -s 'http://x/{a,b}'", "curl -s http://x/[1-3]")


class DenyByDefault(Base):
    def call(self, tool, ti, **kw):
        return self.hook(ds.cmd_agent_tool, bash(tool=tool, tool_input=ti, cwd=self.folder, **kw))

    def test_monitor_runs_only_the_one_check(self):
        for cmd in ("touch /tmp/F", "touch F", "rm -rf x", "while true; do touch F; done", "bash -c 'touch F'", "tail -f a",
                    "datastash watch; touch F", "datastash watch --person --setup x", "datastash api POST /x",
                    SECRET_CMD) + tuple(c for c, _ in BYPASS):
            self.assertTrue(denied(self.call("Monitor", {"command": cmd, "description": "x", "timeout_ms": 1000})),
                            repr(cmd))
        self.assertTrue(denied(self.call("Monitor", {"ws": {"url": "wss://example.invalid/"}, "description": "x",
                                                     "timeout_ms": 1000})))
        self.assertTrue(denied(self.call("Monitor", {"description": "x", "timeout_ms": 1000})))
        for cmd in ("datastash watch", "datastash watch --person", '"${CLAUDE_PLUGIN_ROOT}/scripts/datastash" watch',
                    'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/datastash" watch --person', "datastash status", "ls"):
            self.assertFalse(denied(self.call("Monitor", {"command": cmd, "description": "x", "timeout_ms": 1000})),
                             repr(cmd))
        self.assertTrue(denied(self.call("Bash", {"command": "datastash watch"})))  # a watch never ends: Monitor only

    def test_monitor_touch_never_runs(self):
        import subprocess
        from harness import tmpdir
        d = tmpdir(self, prefix="clean-monitor-")
        cmd = "touch " + os.path.join(d, "F")
        inp = bash(tool="Monitor", tool_input={"command": cmd, "description": "x", "timeout_ms": 1000}, cwd=d)
        if not denied(self.hook(ds.cmd_agent_tool, inp)):  # never: run only what the hook lets through
            subprocess.run(["bash", "-c", cmd], cwd=d, capture_output=True, timeout=10)
        self.assertFalse(os.path.exists(os.path.join(d, "F")))

    def test_powershell_is_refused(self):
        for cmd in ("New-Item F", "Set-Content -Path F -Value x", "ls", "datastash status", "Get-Content a.py"):
            self.assertTrue(denied(self.call("PowerShell", {"command": cmd})), repr(cmd))

    def test_an_unknown_or_future_tool_is_refused(self):
        for tool, ti in (("FutureTool", {"path": "/x"}), ("Skill", {"skill": "x"}), ("ExitPlanMode", {"plan": "x"}),
                         ("ReadMcpResourceTool", {"server": "x", "uri": "x://y"}),
                         ("EnterWorktree", {}), ("Workflow", {"script": "x"}), ("LSP", {}), ("bash", {"command": "ls"}),
                         ("Read ", {"file_path": "/x"}), ("", {}), (None, {}), ("Shell", {"command": "ls"}),
                         ("FutureRunner", {"command": "touch F"}), ("Grep", {"pattern": "x", "command": "touch F"})):
            self.assertTrue(denied(self.call(tool, ti)), repr(tool))

    def test_an_mcp_tool_that_writes_is_refused(self):
        for tool in (DS_MCP + "ledger_rows_upsert", DS_MCP + "ledger_rows_delete", DS_MCP + "ledger_workbook_delete",
                     DS_MCP + "ledger_workbook_create", DS_MCP + "ledger_inbox_send", DS_MCP + "ledger_agent_status",
                     DS_MCP + "ledger_future_tool", DS_MCP + "sync", "mcp__claude_ai_datastash__ledger_rows_upsert",
                     "mcp__claude_ai_Ledger__ledger_import", "mcp__github__create_pull_request",
                     "mcp__filesystem__write_file", "mcp__evil__rows_get", "mcp__evil__checkin", "mcp__evil__chat_save",
                     "mcp__x", "mcp____ledger_help", "mcp__"):
            self.assertTrue(denied(self.call(tool, {"workbook_id": "w", "rows": []})), tool)

    def test_the_datastash_connectors_read_only_tools_run(self):
        for name in ds.CLEAN_DS_READ_TOOLS:
            for server in (DS_MCP, "mcp__claude_ai_datastash__") + (("mcp__claude_ai_Ledger__",) if name[:7] == "ledger_" else ()):
                self.assertFalse(denied(self.call(server + name, {"workbook_id": "w"})), server + name)

    def test_the_plugins_own_connector_steps_still_run(self):
        for name in ds.CLEAN_DS_STEP_TOOLS:  # each one checked or written by the plugin itself (not refused here)
            for server in (DS_MCP, "mcp__claude_ai_datastash__"):
                self.assertNotEqual((((self.call(server + name, {"name": "x"}) or {}).get("hookSpecificOutput") or {})
                                     .get("permissionDecisionReason")), BLOCK, server + name)

    def test_globs_and_braces_are_refused(self):
        for cmd in GLOBS:
            for no_code in (False, True):
                self.assertFalse(ds.clean_lookup_ok(cmd, no_code, cwd=self.folder), repr(cmd))
            self.assertTrue(denied(self.call("Bash", {"command": cmd})), repr(cmd))
            self.assertTrue(denied(self.call("Monitor", {"command": cmd, "description": "x", "timeout_ms": 1000})),
                            repr(cmd))
        for cmd in ("cat '/pro?/self/environ'", "ls '*'", "grep -r --include='*.py' x ."):  # quoted: literal to bash
            self.assertTrue(ds.clean_lookup_ok(cmd, cwd=self.folder), repr(cmd))

    def test_the_allowed_list_still_runs_in_the_main_session(self):
        self.assertEqual(set(ALLOWED_INPUTS), set(ds.CLEAN_ALLOWED_TOOLS))
        for tool, ti in ALLOWED_INPUTS.items():
            self.assertFalse(denied(self.call(tool, ti)), tool)
        for cmd in READ_ONLY:
            self.assertFalse(denied(self.call("Bash", {"command": cmd})), repr(cmd))

    # 1.0.3 review MEDIUM: one test per tool added to the main session's list; each runs, and a command key still
    # sends it through the command check (refused).
    def _added_tool_runs(self, tool):
        self.assertIn(tool, ds.CLEAN_ALLOWED_TOOLS)
        self.assertFalse(denied(self.call(tool, ALLOWED_INPUTS[tool])), tool)
        self.assertTrue(denied(self.call(tool, dict(ALLOWED_INPUTS[tool], command="touch F"))), tool)
        self.assertTrue(denied(self.call(tool.lower(), ALLOWED_INPUTS[tool])), tool)  # names match exactly

    def test_enter_plan_mode_runs(self):
        self._added_tool_runs("EnterPlanMode")

    def test_todo_write_runs(self):
        self._added_tool_runs("TodoWrite")

    def test_task_create_runs(self):
        self._added_tool_runs("TaskCreate")

    def test_task_update_runs(self):
        self._added_tool_runs("TaskUpdate")

    def test_task_list_runs(self):
        self._added_tool_runs("TaskList")

    def test_task_get_runs(self):
        self._added_tool_runs("TaskGet")

    def test_task_output_runs(self):
        self._added_tool_runs("TaskOutput")

    def test_list_mcp_resources_runs(self):
        self._added_tool_runs("ListMcpResourcesTool")

    def test_exit_plan_mode_skill_and_read_mcp_resource_stay_refused(self):
        for tool, ti in (("ExitPlanMode", {"plan": "x"}), ("Skill", {"skill": "x"}),
                         ("ReadMcpResourceTool", {"server": "x", "uri": "x://y"})):
            self.assertNotIn(tool, ds.CLEAN_ALLOWED_TOOLS)
            self.assertTrue(denied(self.call(tool, ti)), tool)
            self.assertFalse(denied(self.call(tool, ti, agent_id="a1b2c3")), tool)

    def test_a_subagent_is_unaffected(self):
        for tool, ti in (("Monitor", {"command": "touch /tmp/F", "description": "x", "timeout_ms": 1000}),
                         ("PowerShell", {"command": "New-Item F"}), ("FutureTool", {"path": "/x"}),
                         ("Skill", {"skill": "x"}), (DS_MCP + "ledger_rows_upsert", {"workbook_id": "w"}),
                         ("mcp__github__create_pull_request", {}), ("Bash", {"command": "cat /pro?/self/environ"}),
                         ("Bash", {"command": "ls", "run_in_background": True}), ("Write", WRITE_TOOLS["Write"])):
            self.assertFalse(denied(self.call(tool, ti, agent_id="a1b2c3")), tool)

    def test_clean_sessions_off_refuses_nothing(self):
        ds.set_clean(commands=False, no_code=False)
        for tool, ti in (("Monitor", {"command": "touch F", "description": "x", "timeout_ms": 1000}),
                         ("PowerShell", {"command": "New-Item F"}), ("FutureTool", {})):
            self.assertFalse(denied(self.call(tool, ti)), tool)

    def test_a_check_that_breaks_fails_closed_in_the_main_session(self):
        saved = ds.clean_settings
        ds.clean_settings = lambda: 1 / 0
        try:
            self.assertTrue(denied(self.call("Bash", {"command": "ls"})))
            self.assertTrue(denied(self.call("Monitor", {"command": "datastash watch", "description": "x",
                                                         "timeout_ms": 1000})))
            self.assertFalse(denied(self.call("Agent", {"prompt": "do it"})))  # the hand-off still works
            self.assertFalse(denied(self.call("Bash", {"command": "touch F"}, agent_id="a1b2c3")))
        finally:
            ds.clean_settings = saved


class Phrases(Base):
    def prompt(self, text, sid="sess1"):
        return self.hook(ds.cmd_prompt, {"session_id": sid, "hook_event_name": "UserPromptSubmit", "prompt": text,
                                         "cwd": self.folder, "prompt_id": str(uuid.uuid4()),
                                         "transcript_path": self.transcript(sid)})

    def test_the_phrases_are_recognised_forgivingly(self):
        self.assertEqual(ds.clean_phrase("no code in sessions"), "no_code")
        self.assertEqual(ds.clean_phrase('  "No code in sessions."'), "no_code")
        self.assertEqual(ds.clean_phrase("no code in my session"), "no_code")
        self.assertEqual(ds.clean_phrase("Turn off clean sessions"), "off")
        self.assertEqual(ds.clean_phrase("turn on clean sessions!"), "on")
        self.assertEqual(ds.clean_phrase("code in sessions"), "code")
        for t in ("please explain why there is no code in sessions here", "clean sessions", "", None, "x" * 500):
            self.assertIsNone(ds.clean_phrase(t), t)

    def test_no_code_in_sessions_turns_on_the_second_choice_and_the_agent_confirms(self):
        out = self.prompt("no code in sessions")
        self.assertEqual(ds.clean_settings(), {"commands": True, "no_code": True})
        ctx = ctx_of(out)
        self.assertIn(ds.CLEAN_NOTE, ctx)
        self.assertIn("one line", ctx)
        self.assertIn("code blocks", ctx)  # the rule applies in this session at once

    def test_turn_off_turns_both_off(self):
        ds.set_clean(no_code=True)
        out = self.prompt("turn off clean sessions")
        self.assertEqual(ds.clean_settings(), {"commands": False, "no_code": False})
        self.assertIn("one line", ctx_of(out))
        self.assertIn("off", ctx_of(out))

    def test_turn_on_turns_commands_back_on(self):
        ds.set_clean(commands=False)
        out = self.prompt("turn on clean sessions")
        self.assertEqual(ds.clean_settings(), {"commands": True, "no_code": False})
        self.assertIn("no code in sessions", ctx_of(out))  # how to turn on the second choice

    def test_code_in_sessions_turns_the_second_choice_off(self):
        ds.set_clean(no_code=True)
        self.prompt("code in sessions")
        self.assertEqual(ds.clean_settings(), {"commands": True, "no_code": False})

    def test_an_ordinary_prompt_changes_nothing(self):
        self.prompt("can you turn off clean sessions for the other project later")
        self.assertEqual(ds.clean_settings(), {"commands": True, "no_code": False})


class Skill(unittest.TestCase):
    def test_skill_names_the_phrases_and_follows_the_note(self):
        with open(SKILL) as f:
            body = f.read().split("# Part 2:", 1)[1]
        self.assertTrue("## Clean sessions" in body, "no Clean sessions section in Part 2")
        for phrase in ('"no code in sessions"', '"turn off clean sessions"', '"turn on clean sessions"'):
            self.assertIn(phrase, body)
        self.assertIn(BLOCK, body)
        self.assertIn("Agent tool", body)
        self.assertNotIn("never a block", body)


if __name__ == "__main__":
    unittest.main()
