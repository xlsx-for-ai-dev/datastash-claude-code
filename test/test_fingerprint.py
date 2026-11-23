#!/usr/bin/env python3
"""Offline tests for XLI-379 PR 3 (plugin 0.9.10): `datastash fingerprint`, the inputs an agent saves a result with
(ledger_agent_result save) and checks it against (check's current).
- A tracked file's fp is its git blob SHA as it is on disk now (equal to git's own hash-object, also after an edit); an
  untracked file or one outside git gets sha256. A folder is all its files; a missing path is gone: true.
- The repo is owner/name from the remote, never its host, user or token; the HEAD commit comes with its branch.
- --url and --lookup hash what is piped in on stdin. Only fingerprints are printed, never a file's text.
- The plugin's own files are refused (also pending/ and a hard link to one); a FIFO is never waited on; no network.
- A file outside any repo is sent as ~/... under the home folder, never with the home folder's absolute path.
- --url and --lookup read stdin in chunks, never whole.
No network, no key; a throwaway HOME and config folder, enforced by test/harness.py.
Run: python3 -I test/test_fingerprint.py"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_status import ds, BIN, TMP  # noqa: E402  (the same harness and plugin copy)

SECRET_TEXT = "FILE_TEXT_CANARY_4b1d"  # inside the files: must never be in the output


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                           "-C", cwd] + list(args), capture_output=True, text=True, check=True).stdout.strip()


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Fingerprint(unittest.TestCase):
    def setUp(self):
        self.repo = os.path.realpath(tempfile.mkdtemp(prefix="fp-repo-", dir=TMP))
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "remote", "add", "origin", "https://someone:ghp_TOKENCANARY@github.com/acme/widgets.git")
        for rel, text in (("src/a.ts", f"export const a = '{SECRET_TEXT}';\n"), ("src/b.ts", "b\n"), ("README.md", "r\n")):
            os.makedirs(os.path.dirname(os.path.join(self.repo, rel)), exist_ok=True)
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write(text)
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write("ignored/\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "one")
        self.head = git(self.repo, "rev-parse", "HEAD")
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)

    def run_bin(self, *args, stdin=None, cwd=None):
        env = dict(os.environ, DATASTASH_NO_NETWORK="1")
        r = subprocess.run([sys.executable, "-I", BIN, "fingerprint"] + list(args), input=stdin, capture_output=True,
                           env=env, cwd=cwd or self.repo, timeout=30)
        return r.returncode, r.stdout.decode(), r.stderr.decode()

    def items(self, *args, **kw):
        rc, out, err = self.run_bin(*args, **kw)
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def test_tracked_files_get_gits_own_blob_sha_with_repo_and_path(self):
        got = self.items("src/a.ts", "README.md")
        files = [i for i in got if i["kind"] == "file"]
        self.assertEqual([(i["repo"], i["path"]) for i in files], [("acme/widgets", "src/a.ts"), ("acme/widgets", "README.md")])
        for i in files:
            self.assertEqual(i["fp"], git(self.repo, "hash-object", i["path"]))
            self.assertEqual(i["fp"], git(self.repo, "rev-parse", f"HEAD:{i['path']}"))
        self.assertEqual(got[-1], {"kind": "commit", "repo": "acme/widgets", "sha": self.head, "ref": "main"})

    def test_an_edit_changes_the_fp_to_gits_hash_of_the_file_now(self):
        before = self.items("--no-commit", "src/b.ts")[0]["fp"]
        with open(os.path.join(self.repo, "src", "b.ts"), "w") as f:
            f.write("b changed\n")
        after = self.items("--no-commit", "src/b.ts")[0]["fp"]
        self.assertNotEqual(before, after)
        self.assertEqual(after, git(self.repo, "hash-object", "src/b.ts"))
        self.assertEqual(self.items("--no-commit", "src/b.ts")[0]["fp"], after)  # stable

    def test_untracked_and_outside_files_get_sha256(self):
        with open(os.path.join(self.repo, "new.txt"), "w") as f:
            f.write("new\n")
        outside = os.path.join(os.path.realpath(tempfile.mkdtemp(prefix="fp-plain-", dir=TMP)), "notes.md")
        with open(outside, "w") as f:
            f.write("notes\n")
        got = self.items("--no-commit", "new.txt", outside)
        self.assertEqual(got[0], {"kind": "file", "repo": "acme/widgets", "path": "new.txt",
                                  "fp": sha256(os.path.join(self.repo, "new.txt"))})
        self.assertEqual(got[1], {"kind": "file", "repo": "local", "path": outside, "fp": sha256(outside)})

    def test_a_folder_is_its_files_and_ignored_files_stay_out(self):
        os.makedirs(os.path.join(self.repo, "ignored"))
        with open(os.path.join(self.repo, "ignored", "x.ts"), "w") as f:
            f.write("x\n")
        with open(os.path.join(self.repo, "src", "c.ts"), "w") as f:
            f.write("c\n")
        got = self.items("--no-commit", "src")
        self.assertEqual([i["path"] for i in got], ["src/a.ts", "src/b.ts", "src/c.ts"])
        self.assertEqual(got[2]["fp"], sha256(os.path.join(self.repo, "src", "c.ts")))  # untracked
        whole = [i["path"] for i in self.items("--no-commit", ".")]
        self.assertNotIn("ignored/x.ts", whole)
        self.assertIn(".gitignore", whole)
        # From a subfolder, paths stay relative to the repo's top.
        self.assertEqual(self.items("--no-commit", "a.ts", cwd=os.path.join(self.repo, "src"))[0]["path"], "src/a.ts")

    def test_a_missing_path_is_gone_for_check(self):
        got = self.items("--no-commit", "src/deleted.ts")
        self.assertEqual(got, [{"kind": "file", "repo": "acme/widgets", "path": "src/deleted.ts", "gone": True}])

    def test_no_paths_gives_this_folders_commit_only(self):
        self.assertEqual(self.items(), [{"kind": "commit", "repo": "acme/widgets", "sha": self.head, "ref": "main"}])
        git(self.repo, "checkout", "-q", "--detach")
        self.assertEqual(self.items(), [{"kind": "commit", "repo": "acme/widgets", "sha": self.head}])

    def test_the_repo_name_never_carries_host_user_or_token(self):
        rc, out, err = self.run_bin("src/a.ts")
        self.assertNotIn("TOKENCANARY", out + err)
        self.assertNotIn("someone", out + err)
        self.assertNotIn("github.com", out)
        git(self.repo, "remote", "remove", "origin")
        git(self.repo, "remote", "add", "upstream", "git@github.com:acme/other.git")
        self.assertEqual(self.items("--no-commit", "src/a.ts")[0]["repo"], "acme/other")
        git(self.repo, "remote", "remove", "upstream")
        self.assertEqual(self.items("--no-commit", "src/a.ts")[0]["repo"], "local/" + os.path.basename(self.repo))

    def test_never_a_files_text_in_the_output(self):
        rc, out, err = self.run_bin("src", "README.md")
        self.assertEqual(rc, 0, err)
        self.assertNotIn(SECRET_TEXT, out + err)

    def test_url_and_lookup_hash_stdin(self):
        page = b"<html>page " + SECRET_TEXT.encode() + b"</html>"
        got = self.items("--no-commit", "--url", "https://example.com/docs?x=1", stdin=page)
        self.assertEqual(got, [{"kind": "url", "url": "https://example.com/docs?x=1", "fp": hashlib.sha256(page).hexdigest()}])
        ans = b'{"state":"OPEN","headRefOid":"abc"}\n'
        got = self.items("--no-commit", "--lookup", "gh:pr/449", stdin=ans)
        self.assertEqual(got, [{"kind": "note", "label": "gh:pr/449", "fp": hashlib.sha256(ans).hexdigest()}])
        got = self.items("src/b.ts", "--lookup", "linear:XLI-379", stdin=b"x")
        self.assertEqual([i["kind"] for i in got], ["file", "commit", "note"])

    def test_refusals(self):
        rc, out, err = self.run_bin("--url", "https://u:p@example.com/", stdin=b"x")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("user and password", err)
        rc, out, err = self.run_bin("--url", "file:///etc/passwd", stdin=b"x")
        self.assertEqual(rc, 1)
        rc, out, err = self.run_bin("--url", "https://a.example/", "--lookup", "k", stdin=b"x")
        self.assertEqual(rc, 1)
        self.assertIn("one --url or --lookup", err)
        rc, out, err = self.run_bin("--bogus")
        self.assertEqual(rc, 1)
        self.assertIn("usage: datastash fingerprint", err)

    def test_the_plugins_own_files_are_never_fingerprinted(self):
        ds.ensure_dirs()
        key = os.path.join(ds.CONF, "agent-fp-test.key")
        with open(key, "w") as f:
            f.write("not a real key\n")
        self.addCleanup(lambda: os.replace(key, os.path.join(self.repo, "moved.key")))
        for target in (key, os.path.join(ds.STATE)):
            rc, out, err = self.run_bin("--no-commit", target)
            self.assertEqual((rc, out), (1, ""), target)
            self.assertIn("never fingerprinted", err)
        link = os.path.join(self.repo, "innocent.txt")
        os.symlink(key, link)
        rc, out, err = self.run_bin("--no-commit", "innocent.txt")
        self.assertEqual((rc, out), (1, ""))

    def test_the_pending_folder_is_refused(self):
        ds.ensure_dirs()
        os.makedirs(ds.PENDING, mode=0o700, exist_ok=True)
        code = os.path.join(ds.PENDING, "hook-fp-test.code")
        with open(code, "w") as f:
            f.write("not a real code\n")
        self.addCleanup(lambda: os.replace(code, os.path.join(self.repo, "moved.code")))
        for target in (code, ds.PENDING):
            rc, out, err = self.run_bin("--no-commit", target)
            self.assertEqual((rc, out), (1, ""), target)
            self.assertIn("never fingerprinted", err)
        self.assertTrue(ds._fp_own_file(os.path.realpath(code)))

    def test_a_hard_link_to_a_protected_file_is_refused(self):
        ds.ensure_dirs()
        os.makedirs(ds.PENDING, mode=0o700, exist_ok=True)
        key = os.path.join(ds.CONF, "agent-fp-hardlink.key")
        code = os.path.join(ds.PENDING, "hook-fp-hardlink.code")
        state = os.path.join(ds.STATE, "fp-hardlink.json")
        moved = os.path.realpath(tempfile.mkdtemp(prefix="fp-moved-", dir=TMP))  # outside the repo (no rm)
        for i, path in enumerate((key, code, state)):
            with open(path, "w") as f:
                f.write(f"not a real secret {SECRET_TEXT}\n")
            self.addCleanup(lambda path=path, i=i: os.replace(path, os.path.join(self.repo, f"moved-{i}")))
            link = os.path.join(self.repo, f"innocent-{i}.txt")
            try:
                os.link(path, link)
            except OSError as e:
                self.skipTest(f"no hard links here ({e})")
            rc, out, err = self.run_bin("--no-commit", link)
            self.assertEqual((rc, out), (1, ""), path)
            self.assertIn("never fingerprinted", err)
            self.assertNotIn(SECRET_TEXT, out + err)
            # inside a folder too, and never partly printed
            rc, out, err = self.run_bin("--no-commit", ".")
            self.assertEqual((rc, out), (1, ""), path)
            os.replace(link, os.path.join(moved, f"link-{i}"))
        # an ordinary file with two names is still fingerprinted
        plain = os.path.join(self.repo, "plain.txt")
        with open(plain, "w") as f:
            f.write("plain\n")
        os.link(plain, os.path.join(self.repo, "plain2.txt"))
        got = self.items("--no-commit", "plain2.txt")
        self.assertEqual(got[0]["fp"], sha256(plain))

    def test_a_file_outside_a_repo_is_sent_home_relative(self):
        home = os.path.realpath(os.path.expanduser("~"))
        folder = os.path.realpath(tempfile.mkdtemp(prefix="fp-home-", dir=home))
        notes = os.path.join(folder, "sub", "notes.md")
        os.makedirs(os.path.dirname(notes))
        with open(notes, "w") as f:
            f.write("notes\n")
        shown = "~/" + os.path.relpath(notes, home).replace(os.sep, "/")
        for cwd in (self.repo, folder):
            got = self.items("--no-commit", notes, cwd=cwd)
            self.assertEqual(got, [{"kind": "file", "repo": "local", "path": shown, "fp": sha256(notes)}])
        got = self.items("--no-commit", folder)  # a folder outside git: each file home-relative
        self.assertEqual([i["path"] for i in got], [shown])
        got = self.items("--no-commit", os.path.join(folder, "gone.md"))
        self.assertEqual(got[0]["path"], "~/" + os.path.relpath(folder, home) + "/gone.md")
        rc, out, err = self.run_bin("--no-commit", notes, folder)
        self.assertEqual(rc, 0, err)
        self.assertNotIn(home, out)
        # in-process, with the real code path
        self.assertEqual(ds._fp_shown_path(notes), shown)
        self.assertEqual(ds._fp_shown_path("/opt/x/y.txt"), "/opt/x/y.txt")
        self.assertEqual(ds._fp_shown_path(home + "-other/z"), home + "-other/z")  # a sibling is not under home

    def test_url_and_lookup_stream_stdin_in_chunks(self):
        class Stream:
            def __init__(self, data):
                self.data, self.sizes = data, []

            def read(self, n=-1):
                self.sizes.append(n)
                if n is None or n < 0:
                    raise AssertionError("stdin read whole")
                b, self.data = self.data[:n], self.data[n:]
                return b

        page = os.urandom(2 * ds.FP_CHUNK + 5)
        for kw in ({"url": "https://example.com/p"}, {"lookup": "gh:pr/1"}):
            src = Stream(page)
            got = ds.fingerprints([], commit=False, data=src, **kw)
            self.assertEqual(got[0]["fp"], hashlib.sha256(page).hexdigest())
            self.assertTrue(src.sizes and all(0 < n <= ds.FP_CHUNK for n in src.sizes), src.sizes)
            self.assertGreaterEqual(len(src.sizes), 3)
        # the real command, a stdin bigger than one chunk
        got = self.items("--no-commit", "--lookup", "k", stdin=page)
        self.assertEqual(got[0]["fp"], hashlib.sha256(page).hexdigest())
        # the stream is not read when the address is refused
        src = Stream(b"x")
        with self.assertRaises(ValueError):
            ds.fingerprints([], commit=False, url="file:///etc/passwd", data=src)
        self.assertEqual(src.sizes, [])

    def test_a_fifo_is_refused_not_waited_on(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("no FIFOs here")
        os.mkfifo(os.path.join(self.repo, "pipe"))
        rc, out, err = self.run_bin("--no-commit", "pipe")
        self.assertEqual(rc, 1)
        self.assertIn("not a regular file", err)

    def test_in_process_no_network_and_a_big_file(self):
        big = os.path.join(self.repo, "big.bin")
        with open(big, "wb") as f:
            f.write(os.urandom(3 * 1024 * 1024 + 17))
        git(self.repo, "add", "big.bin")
        calls = []
        real = ds.http
        ds.http = lambda *a, **k: calls.append(a) or self.fail("fingerprint made a request")
        try:
            got = ds.fingerprints(["big.bin"], commit=False)
        finally:
            ds.http = real
        self.assertEqual(got[0]["fp"], git(self.repo, "hash-object", "big.bin"))
        self.assertEqual(calls, [])

    def test_help_and_skill_name_it(self):
        env = dict(os.environ, DATASTASH_NO_NETWORK="1")
        r = subprocess.run([sys.executable, "-I", BIN, "help"], capture_output=True, text=True, env=env, timeout=20)
        self.assertIn("fingerprint [PATH ...]", r.stdout)
        with open(os.path.join(HERE, "..", "plugins", "datastash", "skills", "datastash-agent", "SKILL.md")) as f:
            skill = f.read()
        part2 = skill.split("# Part 2:", 1)[1]
        self.assertIn("datastash fingerprint", part2)
        self.assertNotIn("datastash fingerprint", skill.split("# Part 2:", 1)[0])
        i = part2.index("## Redo only what changed")
        section = part2[i:i + 2000]
        for needle in ("ledger_agent_result", "`check`", "`reused`", "every input the result rests on", "never file text",
                       "never instructions", "still reads the whole diff"):
            self.assertIn(needle, section, needle)


if __name__ == "__main__":
    unittest.main()
